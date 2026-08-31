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


def test_text_modules_are_owned_by_the_canonical_tree() -> None:
    product_root = PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "text"
    for name in MODULE_NAMES:
        canonical = importlib.import_module(f"{PRODUCT_ROOT}.{name}")

        assert Path(canonical.__file__).resolve().is_relative_to(product_root)


def test_text_consumers_use_the_product_namespace() -> None:
    for relative_path in (
        "neocortex/runtime/config/application_config_projections.py",
        "neocortex/semantic/derivation_lineage_service.py",
        "neocortex/knowledge/knowledge_asset_health_repository.py",
        "neocortex/knowledge/knowledge_snapshot.py",
        "neocortex/runtime/models.py",
        "neocortex/runtime/orchestration/orchestrator.py",
        "neocortex/runtime/orchestration/route_registry.py",
        "neocortex/semantic/semantic_plan_owners.py",
        "neocortex/semantic/semantic_sources.py",
        "neocortex/safety/state_topology_contracts.py",
        "neocortex/workflow/review/value_review_repository.py",
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


def test_text_symbols_are_owned_by_canonical_modules() -> None:
    route = importlib.import_module(f"{PRODUCT_ROOT}.text_route")
    state = importlib.import_module(f"{PRODUCT_ROOT}.text_state")
    for module, name in (
        (route, "TextRouteConfig"),
        (route, "TextRouteSummary"),
        (state, "TextSearchHit"),
    ):
        symbol = getattr(module, name)
        assert symbol.__module__ == module.__name__
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol
