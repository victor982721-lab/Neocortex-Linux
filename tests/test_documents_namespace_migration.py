"""Compatibility contracts for the canonical Documents namespace."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS_ROOT = PROJECT_ROOT / "neocortex" / "documents"
MODULES = tuple(sorted(path.stem for path in DOCUMENTS_ROOT.glob("document*.py")))


def test_document_modules_are_owned_by_the_canonical_tree() -> None:
    assert len(MODULES) == 19
    assert {"document_organization_scope", "document_resource_binding"} <= set(MODULES)
    for name in MODULES:
        product = __import__(f"neocortex.documents.{name}", fromlist=[name])
        assert Path(product.__file__).resolve().is_relative_to(DOCUMENTS_ROOT)


def test_documents_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.documents")
forbidden = {
    "neocortex.documents.document_catalog",
    "neocortex.documents.document_taxonomy",
    "neocortex.documents.document_organization_application",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("documents loaded eagerly: " + ",".join(loaded))
print("DOCUMENTS_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "DOCUMENTS_IMPORT_LIGHT"
