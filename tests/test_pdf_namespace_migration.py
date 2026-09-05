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


TEST_CAPABILITIES = ("base", "documents")


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


@pytest.mark.parametrize("name", [
    pytest.param(name, marks=pytest.mark.capability("documents"))
    if name in {"pdf_derived", "pdf_isolation", "pdf_profile", "pdf_route"} else name
    for name in MODULE_NAMES
])
def test_pdf_modules_are_owned_by_the_canonical_tree(name: str) -> None:
    canonical = importlib.import_module(f"{PRODUCT_ROOT}.{name}")

    assert Path(canonical.__file__).resolve().is_relative_to(
        PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "pdf"
    )


def test_pdf_consumers_use_the_product_namespace() -> None:
    for relative_path in (
        "neocortex/api/public.py",
        "neocortex/runtime/config/application_config_projections.py",
        "neocortex/api/cli/cli_direct.py",
        "neocortex/knowledge/knowledge_asset_health_pdf.py",
        "neocortex/knowledge/knowledge_asset_health_repository.py",
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
        pytest.param("pdf_derived", "PdfDerivedSummary", marks=pytest.mark.capability("documents")),
        pytest.param("pdf_route", "PdfRoute", marks=pytest.mark.capability("documents")),
        ("pdf_route_models", "PdfRouteConfig"),
        ("pdf_route_models", "PdfRouteSummary"),
    ),
)
def test_pdf_symbols_keep_historical_pickle_fqns(module_name: str, symbol_name: str) -> None:
    module = importlib.import_module(f"{PRODUCT_ROOT}.{module_name}")
    symbol = getattr(module, symbol_name)
    assert symbol.__module__ == module.__name__
    assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol
