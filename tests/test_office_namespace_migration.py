"""Compatibility contracts for the capability-owned Office namespace."""

from __future__ import annotations

import importlib
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import get_type_hints

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROOT = "neocortex.capabilities.formats.office"
PRODUCT_ROOT = "neocortex.capabilities.formats.office"
MODULE_NAMES = (
    "extraction",
    "extraction_support",
    "legacy_worker",
    "models",
    "route",
    "state",
    "xlsx",
)
HISTORICAL_SYMBOLS = {
    "legacy_worker": ("main",),
    "route": ("OfficeRoute", "OfficeRouteConfig", "OfficeRouteSummary"),
    "state": ("office_database", "initialize_office_state", "search_office_state"),
}


def _canonical_module(name: str):
    return importlib.import_module(f"{CANONICAL_ROOT}.{name}")


def test_office_implementation_lives_under_the_product_namespace() -> None:
    product_root = PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "office"
    for name in MODULE_NAMES:
        module = importlib.import_module(f"{PRODUCT_ROOT}.{name}")
        assert Path(module.__file__).resolve().is_relative_to(product_root)

    for relative_path in (
        "neocortex/api/public.py",
        "neocortex/runtime/config/application_config_projections.py",
        "neocortex/api/cli/cli_direct.py",
        "neocortex/knowledge/knowledge_snapshot.py",
        "neocortex/runtime/models.py",
        "neocortex/runtime/orchestration/orchestrator.py",
        "neocortex/runtime/orchestration/route_registry.py",
        "neocortex/semantic/semantic_plan_owners.py",
        "neocortex/safety/state_topology_contracts.py",
        "neocortex/workflow/review/value_review_repository.py",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert PRODUCT_ROOT in source


@pytest.mark.parametrize("name", ("legacy_worker", "route", "state"))
def test_office_symbols_are_owned_by_canonical_modules(name: str) -> None:
    module = _canonical_module(name)

    for symbol_name in HISTORICAL_SYMBOLS[name]:
        symbol = getattr(module, symbol_name)
        assert symbol.__module__ == module.__name__
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol


def test_office_summary_instance_remains_pickle_compatible() -> None:
    route = _canonical_module("route")
    summary = route.OfficeRouteSummary()

    restored = pickle.loads(pickle.dumps(summary, protocol=5))

    assert restored == summary
    assert type(restored) is route.OfficeRouteSummary
    assert type(restored).__module__ == "neocortex.capabilities.formats.office.route"
    assert get_type_hints(route.OfficeRouteConfig)["selection"].__name__ == ("CandidateSelection")


def test_office_package_import_is_light_in_a_fresh_process() -> None:
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        importlib.import_module({CANONICAL_ROOT!r})
        forbidden = {{
            {", ".join(repr(f"{CANONICAL_ROOT}.{name}") for name in MODULE_NAMES)},
            "openpyxl",
            "neocortex.deduplication",
        }}
        loaded = forbidden.intersection(sys.modules)
        if loaded:
            raise SystemExit("Office package eagerly loaded: " + ",".join(sorted(loaded)))
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


def test_office_route_and_state_contracts_remain_stable() -> None:
    route = _canonical_module("route")
    state = _canonical_module("state")

    assert route.OFFICE_ROUTE_VERSION == "office-route-v2"
    assert state.OFFICE_SCHEMA_VERSION == 3
    assert set(route.OFFICE_MIME_FORMATS.values()) == {"xlsx", "pptx", "odt"}
