"""Compatibility contracts for the physical PDF namespace migration."""

from __future__ import annotations

import importlib
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = "neocortex.capabilities.formats.pdf"
MODULE_NAMES = (
    "pdf_admin",
    "pdf_cache",
    "pdf_derived",
    "pdf_derived_queries",
    "pdf_derived_schema",
    "pdf_isolation",
    "pdf_layout",
    "pdf_profile",
    "pdf_route",
    "pdf_route_cache",
    "pdf_route_models",
    "pdf_route_storage",
    "pdf_runtime",
    "pdf_schema",
    "pdf_state",
    "pdf_writer",
)


def test_legacy_pdf_modules_are_exact_product_aliases() -> None:
    for name in MODULE_NAMES:
        legacy_name = f"_04_Nucleo_Operativo.{name}"
        legacy = importlib.import_module(legacy_name)
        canonical = importlib.import_module(f"{PRODUCT_ROOT}.{name}")

        assert legacy is canonical
        assert sys.modules[legacy_name] is canonical
        assert Path(canonical.__file__).resolve().is_relative_to(
            PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "pdf"
        )


def test_pdf_consumers_use_the_product_namespace() -> None:
    for relative_path in (
        "_04_Nucleo_Operativo/__init__.py",
        "_04_Nucleo_Operativo/application_config_projections.py",
        "_04_Nucleo_Operativo/cli_direct.py",
        "_04_Nucleo_Operativo/code_knowledge_pdf_asset_health_analysis.py",
        "_04_Nucleo_Operativo/knowledge_asset_health_pdf.py",
        "_04_Nucleo_Operativo/knowledge_asset_health_repository.py",
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


def test_pdf_parent_package_remains_import_light() -> None:
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        importlib.import_module({PRODUCT_ROOT!r})
        forbidden = {{
            {", ".join(repr(f"{PRODUCT_ROOT}.{name}") for name in MODULE_NAMES)},
            "fitz",
            "pypdf",
            "neocortex.deduplication",
        }}
        loaded = sorted(forbidden.intersection(sys.modules))
        if loaded:
            raise SystemExit("PDF package eagerly loaded: " + ",".join(loaded))
        print("PDF_PACKAGE_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "PDF_PACKAGE_IMPORT_LIGHT"


@pytest.mark.parametrize(
    ("module_name", "symbol_name"),
    (
        ("pdf_admin", "PdfDoctorReport"),
        ("pdf_derived", "PdfDerivedSummary"),
        ("pdf_route", "PdfRoute"),
        ("pdf_route_models", "PdfRouteConfig"),
        ("pdf_route_models", "PdfRouteSummary"),
    ),
)
def test_pdf_symbols_keep_historical_pickle_fqns(module_name: str, symbol_name: str) -> None:
    module = importlib.import_module(f"{PRODUCT_ROOT}.{module_name}")
    symbol = getattr(module, symbol_name)
    historical = f"_04_Nucleo_Operativo.{module_name}"

    assert symbol.__module__ == historical
    assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol


def test_legacy_pdf_monkeypatch_reaches_canonical_route_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = importlib.import_module("_04_Nucleo_Operativo.pdf_route")
    canonical = importlib.import_module(f"{PRODUCT_ROOT}.pdf_route")
    marker = object()

    monkeypatch.setattr(legacy, "binary_fingerprint", marker)

    assert canonical.binary_fingerprint is marker

