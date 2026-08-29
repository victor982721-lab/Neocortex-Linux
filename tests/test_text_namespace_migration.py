"""Compatibility contracts for the physical Text namespace migration."""

from __future__ import annotations

import importlib
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = "neocortex.capabilities.formats.text"
MODULE_NAMES = ("text_derivation_repository", "text_route", "text_state")


def test_legacy_text_modules_are_exact_product_aliases() -> None:
    product_root = PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "text"
    for name in MODULE_NAMES:
        legacy = importlib.import_module(f"_04_Nucleo_Operativo.{name}")
        canonical = importlib.import_module(f"{PRODUCT_ROOT}.{name}")

        assert legacy is canonical
        assert sys.modules[f"_04_Nucleo_Operativo.{name}"] is canonical
        assert Path(canonical.__file__).resolve().is_relative_to(product_root)


def test_text_consumers_use_the_product_namespace() -> None:
    for relative_path in (
        "_04_Nucleo_Operativo/application_config_projections.py",
        "_04_Nucleo_Operativo/code_capability_reachability_analysis.py",
        "_04_Nucleo_Operativo/code_knowledge_asset_health_analysis.py",
        "_04_Nucleo_Operativo/code_state_projection_analysis.py",
        "_04_Nucleo_Operativo/code_state_topology_analysis.py",
        "_04_Nucleo_Operativo/derivation_lineage_service.py",
        "_04_Nucleo_Operativo/knowledge_asset_health_repository.py",
        "_04_Nucleo_Operativo/knowledge_snapshot.py",
        "_04_Nucleo_Operativo/models.py",
        "_04_Nucleo_Operativo/orchestrator.py",
        "_04_Nucleo_Operativo/route_registry.py",
        "_04_Nucleo_Operativo/semantic_plan_owners.py",
        "_04_Nucleo_Operativo/semantic_sources.py",
        "_04_Nucleo_Operativo/state_topology_contracts.py",
        "_04_Nucleo_Operativo/value_review_repository.py",
    ):
        assert PRODUCT_ROOT in (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_text_parent_package_remains_import_light() -> None:
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        importlib.import_module({PRODUCT_ROOT!r})
        forbidden = {{
            {", ".join(repr(f"{PRODUCT_ROOT}.{name}") for name in MODULE_NAMES)},
            "neocortex.deduplication",
        }}
        loaded = sorted(forbidden.intersection(sys.modules))
        if loaded:
            raise SystemExit("Text package eagerly loaded: " + ",".join(loaded))
        print("TEXT_PACKAGE_IMPORT_LIGHT")
        """
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        (sys.executable, "-B", "-c", script),
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "TEXT_PACKAGE_IMPORT_LIGHT"


def test_text_symbols_keep_historical_pickle_fqns() -> None:
    route = importlib.import_module(f"{PRODUCT_ROOT}.text_route")
    state = importlib.import_module(f"{PRODUCT_ROOT}.text_state")
    for module, name in (
        (route, "TextRouteConfig"),
        (route, "TextRouteSummary"),
        (state, "TextSearchHit"),
    ):
        symbol = getattr(module, name)
        assert symbol.__module__ == f"_04_Nucleo_Operativo.{module.__name__.rsplit('.', 1)[-1]}"
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol

