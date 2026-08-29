"""Executable contracts for the exhaustive Core target architecture."""

from __future__ import annotations

from collections import Counter
from itertools import pairwise
from pathlib import Path

import grimp

from _04_Nucleo_Operativo.code.contracts.target_registry import (
    COMPATIBILITY_CONTRACTS,
    COMPATIBILITY_MODULES,
    COMPATIBILITY_MODULE_PAIRS,
    CORE_COMPATIBILITY_MATRIX_SCHEMA,
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
from _04_Nucleo_Operativo.platform.shared.capability_registry import (
    CAPABILITY_REGISTRY,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = REPOSITORY_ROOT / "_04_Nucleo_Operativo"


def _module_id(path: Path) -> str:
    module = ".".join(path.relative_to(REPOSITORY_ROOT).with_suffix("").parts)
    return module.removesuffix(".__init__")


def test_registry_exhaustively_assigns_every_current_core_module() -> None:
    observed = tuple(sorted(_module_id(path) for path in CORE_ROOT.rglob("*.py")))

    assert observed == registered_core_modules()
    assert len(observed) == 377
    assert len(COMPATIBILITY_MODULES) == 40
    assert sum(len(items) for items in RESPONSIBILITY_MODULES.values()) == 337
    for module in observed:
        families = matching_target_families(module)
        responsibilities = matching_target_responsibilities(module)
        assert len(families) == 1
        if module in COMPATIBILITY_MODULES:
            assert families == ("compat",)
            assert responsibilities == ()
        else:
            assert len(responsibilities) == 1
            assert responsibilities[0].partition(".")[0] == families[0]


def test_target_vocabulary_dag_and_transition_baseline_are_frozen() -> None:
    payload = core_architecture_target_payload()

    assert len(TARGET_RESPONSIBILITY_IDS) == 45
    assert len(TARGET_FAMILIES) == 12
    assert TARGET_FAMILY_DEPENDENCIES == tuple(pairwise(TARGET_FAMILY_LAYERS))
    assert len(FORBIDDEN_FAMILY_EDGE_BASELINE) == 24
    assert sum(item.direct_module_edges for item in FORBIDDEN_FAMILY_EDGE_BASELINE) == 138
    assert payload["responsibility_registry"]["schema"] == (CORE_RESPONSIBILITY_REGISTRY_SCHEMA)
    assert payload["family_dag"]["schema"] == CORE_FAMILY_DAG_SCHEMA
    assert payload["compatibility_matrix"]["schema"] == (CORE_COMPATIBILITY_MATRIX_SCHEMA)
    assert core_architecture_target_fingerprint() == (
        "core-architecture-target-v1:sha256:"
        "dd7468dbfbad48191baf55354f56d82de299dd7f71b91bd05ac6459bfa26cd86"
    )


def test_compatibility_matrix_joins_capability_and_shared_migrations() -> None:
    capability_pairs = {
        (binding.legacy_module_id, binding.canonical_module_id)
        for capability in CAPABILITY_REGISTRY.capabilities
        for binding in capability.modules
        if binding.legacy_module_id is not None
    }
    shared_pairs = {
        (
            "_04_Nucleo_Operativo.content_types",
            "_04_Nucleo_Operativo.platform.shared.content_types",
        ),
        (
            "_04_Nucleo_Operativo.zip_safety",
            "_04_Nucleo_Operativo.platform.shared.zip_safety",
        ),
    }

    assert set(COMPATIBILITY_MODULE_PAIRS) == capability_pairs | shared_pairs
    assert tuple(item.legacy_module_id for item in COMPATIBILITY_CONTRACTS) == (
        COMPATIBILITY_MODULES
    )
    assert all(
        "tests/test_format_module_move_compatibility.py" in item.test_roots
        for item in COMPATIBILITY_CONTRACTS
    )
    image_policy = next(
        item
        for item in COMPATIBILITY_CONTRACTS
        if item.legacy_module_id == "_04_Nucleo_Operativo.image_policy"
    )
    assert "historical_pickle_global" not in image_policy.requirement_ids


def test_forbidden_family_edge_baseline_matches_the_current_import_graph() -> None:
    graph = grimp.build_graph(
        "_04_Nucleo_Operativo",
        include_external_packages=False,
        exclude_type_checking_imports=False,
        cache_dir=None,
    )
    rank = {family: index for index, family in enumerate(TARGET_FAMILY_LAYERS)}
    forbidden: Counter[tuple[str, str]] = Counter()
    canonical_to_compat = 0
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
            if source == target:
                continue
            if source != "compat" and target == "compat":
                canonical_to_compat += 1
            elif rank[source] >= rank[target]:
                forbidden[source, target] += 1

    assert canonical_to_compat == 0
    assert dict(forbidden) == forbidden_family_edge_baseline()
