"""Compatibility contracts for the canonical Knowledge namespace."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_ROOT = PROJECT_ROOT / "neocortex" / "knowledge"

MODULES = tuple(
    sorted(
        path.stem
        for path in KNOWLEDGE_ROOT.glob("knowledge*.py")
    )
)


def test_knowledge_modules_are_owned_by_the_canonical_tree() -> None:
    assert len(MODULES) == 38
    assert {
        "knowledge_asset_diagnosis", "knowledge_asset_diagnosis_contracts",
        "knowledge_asset_diagnosis_owners", "knowledge_context_v2",
        "knowledge_context_hydration", "knowledge_evidence_lookup",
        "knowledge_operational_query",
        "knowledge_read_budget",
        "knowledge_read_operation",
    } <= set(MODULES)
    for name in MODULES:
        product = importlib.import_module(f"neocortex.knowledge.{name}")
        assert Path(product.__file__).resolve().is_relative_to(KNOWLEDGE_ROOT)


def test_knowledge_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.knowledge")
forbidden = {
    "neocortex.knowledge.knowledge_contracts",
    "neocortex.knowledge.knowledge_search",
    "neocortex.knowledge.knowledge_service",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("knowledge loaded eagerly: " + ",".join(loaded))
print("KNOWLEDGE_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "KNOWLEDGE_IMPORT_LIGHT"


def test_knowledge_contracts_keep_historical_pickle_fqns() -> None:
    contracts = importlib.import_module("neocortex.knowledge.knowledge_contracts")
    planner = importlib.import_module("neocortex.knowledge.knowledge_planner")
    identity = importlib.import_module("neocortex.knowledge.knowledge_asset_health_contracts")

    values = (
        contracts.KnowledgeTelemetryClock(signature="fixture"),
        planner.KnowledgeQuery("fixture"),
        identity.KnowledgeAssetIdentity(1, 2, -1),
    )
    for value in values:
        restored = pickle.loads(pickle.dumps(value, protocol=5))
        assert type(restored) is type(value)
        assert restored == value

    assert contracts.KnowledgeTelemetryClock.__module__ == (
        "neocortex.knowledge.knowledge_contracts"
    )
