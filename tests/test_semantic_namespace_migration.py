"""Compatibility contracts for the canonical Semantic namespace."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_ROOT = PROJECT_ROOT / "neocortex" / "semantic"

MODULES = tuple(
    sorted(
        path.stem
        for path in SEMANTIC_ROOT.glob("*.py")
        if path.name != "__init__.py"
    )
)


def test_semantic_modules_are_owned_by_the_canonical_tree() -> None:
    assert len(MODULES) == 37
    for name in MODULES:
        product = importlib.import_module(f"neocortex.semantic.{name}")
        assert Path(product.__file__).resolve().is_relative_to(SEMANTIC_ROOT)


def test_semantic_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.semantic")
forbidden = {
    "neocortex.semantic.semantic_models",
    "neocortex.semantic.semantic_service",
    "neocortex.semantic.semantic_text_index",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("semantic loaded eagerly: " + ",".join(loaded))
print("SEMANTIC_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "SEMANTIC_IMPORT_LIGHT"


def test_semantic_contracts_keep_historical_pickle_fqns() -> None:
    models = importlib.import_module("neocortex.semantic.semantic_models")
    config = importlib.import_module("neocortex.semantic.semantic_config")

    values = (
        models.ContentFingerprint("0" * 32, 1, "0" * 16),
        config.FastEmbedCacheContract("owner/name", ("config.json",)),
    )
    for value in values:
        restored = pickle.loads(pickle.dumps(value, protocol=5))
        assert type(restored) is type(value)
        assert restored == value
