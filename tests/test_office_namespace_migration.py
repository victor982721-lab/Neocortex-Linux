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
CANONICAL_ROOT = "_04_Nucleo_Operativo.capabilities.formats.office"
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
MODULE_MOVES = {
    "legacy_worker": "_04_Nucleo_Operativo.legacy_office_worker",
    "route": "_04_Nucleo_Operativo.office_route",
    "state": "_04_Nucleo_Operativo.office_state",
}
HISTORICAL_SYMBOLS = {
    "legacy_worker": ("main",),
    "route": ("OfficeRoute", "OfficeRouteConfig", "OfficeRouteSummary"),
    "state": ("office_database", "initialize_office_state", "search_office_state"),
}


def _canonical_module(name: str):
    return importlib.import_module(f"{CANONICAL_ROOT}.{name}")


@pytest.mark.parametrize(("name", "legacy_name"), MODULE_MOVES.items())
def test_legacy_office_modules_are_exact_canonical_aliases(
    name: str,
    legacy_name: str,
) -> None:
    canonical = _canonical_module(name)
    legacy = importlib.import_module(legacy_name)

    assert legacy is canonical
    assert sys.modules[legacy_name] is canonical


def test_office_implementation_lives_under_the_product_namespace() -> None:
    product_root = PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "office"
    for name in MODULE_NAMES:
        module = importlib.import_module(f"{PRODUCT_ROOT}.{name}")
        assert Path(module.__file__).resolve().is_relative_to(product_root)

    for relative_path in (
        "_04_Nucleo_Operativo/__init__.py",
        "_04_Nucleo_Operativo/application_config_projections.py",
        "_04_Nucleo_Operativo/cli_direct.py",
        "_04_Nucleo_Operativo/knowledge_snapshot.py",
        "_04_Nucleo_Operativo/models.py",
        "_04_Nucleo_Operativo/orchestrator.py",
        "_04_Nucleo_Operativo/route_registry.py",
        "_04_Nucleo_Operativo/semantic_plan_owners.py",
        "_04_Nucleo_Operativo/state_topology_contracts.py",
        "_04_Nucleo_Operativo/value_review_repository.py",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert PRODUCT_ROOT in source


@pytest.mark.parametrize(("name", "legacy_name"), MODULE_MOVES.items())
def test_office_symbols_keep_historical_pickle_fqns(name: str, legacy_name: str) -> None:
    module = _canonical_module(name)

    for symbol_name in HISTORICAL_SYMBOLS[name]:
        symbol = getattr(module, symbol_name)
        assert symbol.__module__ == legacy_name
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol


def test_office_summary_instance_remains_pickle_compatible() -> None:
    route = _canonical_module("route")
    summary = route.OfficeRouteSummary()

    restored = pickle.loads(pickle.dumps(summary, protocol=5))

    assert restored == summary
    assert type(restored) is route.OfficeRouteSummary
    assert type(restored).__module__ == "_04_Nucleo_Operativo.office_route"
    assert get_type_hints(route.OfficeRouteConfig)["selection"].__name__ == ("CandidateSelection")


def test_legacy_office_monkeypatch_reaches_canonical_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        ("legacy_worker", "os"),
        ("route", "_extract_xlsx_shared_strings"),
        ("state", "_migrate_office_v2_path_collation"),
    )
    sentinel = object()
    for module_name, attribute_name in cases:
        legacy = importlib.import_module(MODULE_MOVES[module_name])
        canonical = _canonical_module(module_name)
        monkeypatch.setattr(legacy, attribute_name, sentinel)
        assert getattr(canonical, attribute_name) is sentinel


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
