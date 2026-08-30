"""Compatibility contracts for the identity/provenance foundation migration."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = PROJECT_ROOT / "neocortex" / "foundation"

LEGACY_ALIASES = {
    "_04_Nucleo_Operativo.file_identity": "neocortex.foundation.file_identity",
    "_04_Nucleo_Operativo.processing_provenance": (
        "neocortex.foundation.processing_provenance"
    ),
}


def test_legacy_foundation_modules_are_exact_product_aliases() -> None:
    for legacy_name, product_name in LEGACY_ALIASES.items():
        legacy = importlib.import_module(legacy_name)
        product = importlib.import_module(product_name)

        assert legacy is product
        assert sys.modules[legacy_name] is product
        assert sys.modules[product_name] is product


def test_foundation_implementation_lives_under_product_namespace() -> None:
    for name in ("file_identity", "processing_provenance"):
        module = importlib.import_module(f"neocortex.foundation.{name}")
        assert Path(module.__file__).resolve().is_relative_to(PRODUCT_ROOT)

    for relative_path in (
        "neocortex/documents/document_catalog.py",
        "neocortex/knowledge/knowledge_asset_health_pdf.py",
        "neocortex/knowledge/knowledge_asset_health_repository.py",
        "neocortex/knowledge/knowledge_exact.py",
        "neocortex/knowledge/knowledge_search.py",
        "neocortex/knowledge/knowledge_search_catalog.py",
        "neocortex/knowledge/knowledge_search_code.py",
        "neocortex/knowledge/knowledge_search_content.py",
        "neocortex/runtime/config/model_management.py",
        "neocortex/semantic/semantic_sources.py",
        "neocortex/capabilities/formats/archive/route.py",
        "neocortex/capabilities/formats/audio/models.py",
        "neocortex/capabilities/formats/docx/models.py",
        "neocortex/capabilities/formats/image/route.py",
        "neocortex/capabilities/formats/office/route.py",
        "neocortex/capabilities/formats/pdf/pdf_admin.py",
        "neocortex/capabilities/formats/text/text_route.py",
        "neocortex/capabilities/formats/video/route.py",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert "neocortex.foundation" in source


def test_foundation_parent_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.foundation")
forbidden = {
    "neocortex.foundation.file_identity",
    "neocortex.foundation.processing_provenance",
    "xxhash",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("foundation package eagerly loaded: " + ",".join(loaded))
print("FOUNDATION_PACKAGE_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "FOUNDATION_PACKAGE_IMPORT_LIGHT"


def test_foundation_symbols_keep_historical_pickle_fqns() -> None:
    identity = importlib.import_module("neocortex.foundation.file_identity")
    provenance = importlib.import_module("neocortex.foundation.processing_provenance")

    symbols = (
        (identity, "FileIdentity", "_04_Nucleo_Operativo.file_identity"),
        (identity, "FileIdentityError", "_04_Nucleo_Operativo.file_identity"),
        (
            provenance,
            "ProcessingProvenance",
            "_04_Nucleo_Operativo.processing_provenance",
        ),
        (
            provenance,
            "TesseractRuntimeProvenance",
            "_04_Nucleo_Operativo.processing_provenance",
        ),
    )
    for module, name, historical_module in symbols:
        symbol = getattr(module, name)
        assert symbol.__module__ == historical_module
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol

    identity_value = identity.FileIdentity(1, 2)
    provenance_value = provenance.ProcessingProvenance("signature", "{}")
    for instance in (identity_value, provenance_value):
        restored = pickle.loads(pickle.dumps(instance, protocol=5))
        assert type(restored) is type(instance)
        assert restored == instance
