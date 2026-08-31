"""Compatibility contracts for the canonical platform namespace migration."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = PROJECT_ROOT / "neocortex" / "platform"

def test_platform_implementation_lives_under_product_namespace() -> None:
    for name in (
        "architecture_projection",
        "capability_registry",
        "capability_registry_specs",
        "content_types",
        "zip_safety",
    ):
        module = importlib.import_module(f"neocortex.platform.{name}")
        assert Path(module.__file__).resolve().is_relative_to(PRODUCT_ROOT)

    for relative_path in (
        "neocortex/workflow/actions/actions.py",
        "neocortex/persistence/framework_state_writer.py",
        "neocortex/capabilities/formats/archive/route.py",
        "neocortex/capabilities/formats/docx/integrity.py",
        "neocortex/capabilities/formats/docx/route.py",
        "neocortex/capabilities/formats/office/extraction.py",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert "neocortex.platform" in source


def test_platform_parent_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.platform")
forbidden = {
    "neocortex.platform.architecture_projection",
    "neocortex.platform.capability_registry",
    "neocortex.platform.capability_registry_specs",
    "neocortex.platform.content_types",
    "neocortex.platform.zip_safety",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("platform package eagerly loaded: " + ",".join(loaded))
print("PLATFORM_PACKAGE_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "PLATFORM_PACKAGE_IMPORT_LIGHT"


def test_platform_symbols_are_owned_by_canonical_modules() -> None:
    projection = importlib.import_module("neocortex.platform.architecture_projection")
    registry = importlib.import_module("neocortex.platform.capability_registry")
    content_types = importlib.import_module("neocortex.platform.content_types")
    zip_safety = importlib.import_module("neocortex.platform.zip_safety")

    symbols = (
        (projection, "ModuleEdge", "neocortex.platform.architecture_projection"),
        (registry, "CapabilitySpec", "neocortex.platform.capability_registry"),
        (content_types, "DetectedType", "neocortex.platform.content_types"),
        (zip_safety, "ZipStructure", "neocortex.platform.zip_safety"),
        (zip_safety, "RawDeflateMember", "neocortex.platform.zip_safety"),
    )
    for module, name, canonical_module in symbols:
        symbol = getattr(module, name)
        assert symbol.__module__ == canonical_module
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol

    instances = (
        projection.ModuleEdge("a", "b", "witness"),
        content_types.DetectedType("text/plain", ".txt", frozenset({".txt"}), "fixture"),
        zip_safety.ZipStructure(1, 2, 3, False),
        zip_safety.RawDeflateMember(b"payload", 7, 123),
    )
    for instance in instances:
        restored = pickle.loads(pickle.dumps(instance, protocol=5))
        assert type(restored) is type(instance)
        assert restored == instance


def test_platform_capability_registry_specs_are_loaded_from_product_file() -> None:
    product = importlib.import_module("neocortex.platform.capability_registry_specs")

    assert product.CAPABILITY_SPEC_PAYLOADS
    assert Path(product.__file__).resolve() == PRODUCT_ROOT / "capability_registry_specs.py"
