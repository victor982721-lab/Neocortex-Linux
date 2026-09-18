"""Compatibility contracts for the canonical Semantic namespace."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path

import pytest


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_ROOT = PROJECT_ROOT / "neocortex" / "semantic"

MODULES = tuple(
    sorted(
        path.stem
        for path in SEMANTIC_ROOT.glob("*.py")
        if path.name != "__init__.py"
    )
)


EXPECTED_MODULES = frozenset(
    {
        "derivation_contracts",
        "derivation_lineage_service",
        "derivation_projection",
        "image_retrieval_calibration",
        "semantic_admission",
        "semantic_backend_supervisor",
        "semantic_backends",
        "semantic_chunking",
        "semantic_classification_service",
        "semantic_config",
        "semantic_contract_payloads",
        "semantic_contract_validation",
        "semantic_evidence_repository",
        "semantic_exact_index",
        "semantic_exact_index_format",
        "semantic_generation_control_schema",
        "semantic_generation_repository",
        "semantic_generation_worker",
        "semantic_image_index",
        "semantic_item_repository",
        "semantic_lexical",
        "semantic_lineage_repository",
        "semantic_models",
        "semantic_ontology",
        "semantic_plan_errors",
        "semantic_plan_owners",
        "semantic_plan_results",
        "semantic_plan_scratch",
        "semantic_planner",
        "semantic_preparation",
        "semantic_publication_heads",
        "semantic_quality",
        "semantic_query_evidence",
        "semantic_query_variants",
        "semantic_repository_common",
        "semantic_schema",
        "semantic_search_order",
        "semantic_search_repository",
        "semantic_search_service",
        "semantic_service",
        "semantic_service_contracts",
        "semantic_source_budget",
        "semantic_source_head_cache",
        "semantic_sources",
        "semantic_state",
        "semantic_status_service",
        "semantic_text_index",
        "semantic_vector_search",
        "semantic_work_budget",
        "video_source",
    }
)
INTENTIONAL_NEW_MODULES = frozenset(
    {"semantic_search_order", "semantic_vector_search"}
)


def test_semantic_modules_are_owned_by_the_canonical_tree() -> None:
    assert set(MODULES) == EXPECTED_MODULES
    assert INTENTIONAL_NEW_MODULES <= set(MODULES)
    assert not tuple(PROJECT_ROOT.glob("neocortex/semantic_*.py"))
    for name in MODULES:
        product = importlib.import_module(f"neocortex.semantic.{name}")
        assert Path(product.__file__).resolve() == (
            SEMANTIC_ROOT / f"{name}.py"
        ).resolve()


def test_semantic_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.semantic")
forbidden = {
    "neocortex.semantic.semantic_models",
    "neocortex.semantic.semantic_service",
    "neocortex.semantic.semantic_text_index",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("semantic loaded eagerly: " + ",".join(loaded))
print("SEMANTIC_IMPORT_LIGHT")
"""
    completed = subprocess.run(
        (sys.executable, "-B", "-c", script),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "SEMANTIC_IMPORT_LIGHT"


def test_semantic_contracts_keep_historical_pickle_fqns() -> None:
    models = importlib.import_module("neocortex.semantic.semantic_models")
    config = importlib.import_module("neocortex.semantic.semantic_config")

    values = (
        models.ContentFingerprint("0" * 32, 1, "0" * 16),
        config.FastEmbedCacheContract("owner/name", ("config.json",)),
    )
    for value in values:
        restored = pickle.loads(pickle.dumps(value, protocol=5))
        assert type(restored) is type(value)
        assert restored == value
