"""Compatibility contracts for the physical Image namespace migration."""

from __future__ import annotations

import ast
import importlib
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = "neocortex.capabilities.formats.image"

IMAGE_MODULES = (
    "adult",
    "analysis",
    "decision",
    "decode",
    "document",
    "errors",
    "features",
    "isolation",
    "models",
    "png",
    "policy",
    "route",
    "semantics",
    "state",
    "visual",
)
LEGACY_ALIASES = {
    f"_04_Nucleo_Operativo.image_{name}": (
        f"_04_Nucleo_Operativo.capabilities.formats.image.{name}"
    )
    for name in IMAGE_MODULES
}


def test_legacy_image_modules_are_real_canonical_aliases() -> None:
    for legacy_name, canonical_name in LEGACY_ALIASES.items():
        legacy = importlib.import_module(legacy_name)
        canonical = importlib.import_module(canonical_name)

        assert legacy is canonical
        assert sys.modules[legacy_name] is canonical
        assert sys.modules[canonical_name] is canonical


def test_image_implementation_lives_under_the_product_namespace() -> None:
    product_root = PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "image"
    for name in IMAGE_MODULES:
        module = importlib.import_module(f"{PRODUCT_ROOT}.{name}")
        assert Path(module.__file__).resolve().is_relative_to(product_root)

    for relative_path in (
        "_04_Nucleo_Operativo/__init__.py",
        "_04_Nucleo_Operativo/application_config_projections.py",
        "_04_Nucleo_Operativo/cli_video.py",
        "_04_Nucleo_Operativo/knowledge_snapshot.py",
        "_04_Nucleo_Operativo/models.py",
        "_04_Nucleo_Operativo/orchestrator.py",
        "_04_Nucleo_Operativo/route_registry.py",
        "_04_Nucleo_Operativo/semantic_plan_owners.py",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert PRODUCT_ROOT in source


def test_image_parent_packages_remain_import_light() -> None:
    script = textwrap.dedent(
        f"""
        import sys

        import _04_Nucleo_Operativo.capabilities
        import _04_Nucleo_Operativo.capabilities.formats
        import _04_Nucleo_Operativo.capabilities.formats.image

        forbidden = {set(LEGACY_ALIASES) | set(LEGACY_ALIASES.values())!r}
        loaded = sorted(forbidden.intersection(sys.modules))
        loaded_pillow = sorted(
            name for name in sys.modules if name == "PIL" or name.startswith("PIL.")
        )
        if loaded or loaded_pillow:
            raise SystemExit(
                "eager Image imports: " + ",".join((*loaded, *loaded_pillow))
            )
        print("IMAGE_PARENTS_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "IMAGE_PARENTS_IMPORT_LIGHT"


def test_canonical_image_modules_do_not_route_through_legacy_aliases() -> None:
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        for module_name in {tuple(LEGACY_ALIASES.values())!r}:
            importlib.import_module(module_name)
        loaded = sorted(set({tuple(LEGACY_ALIASES)!r}).intersection(sys.modules))
        if loaded:
            raise SystemExit("canonical Image import used legacy aliases: " + ",".join(loaded))
        print("IMAGE_CANONICAL_IMPORTS_ONLY")
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
    assert completed.stdout.strip() == "IMAGE_CANONICAL_IMPORTS_ONLY"


def test_image_definitions_keep_historical_pickle_fqns() -> None:
    definitions_checked = 0
    for legacy_name, canonical_name in LEGACY_ALIASES.items():
        module = importlib.import_module(canonical_name)
        module_path = Path(module.__file__ or "")
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        defined_names = tuple(
            dict.fromkeys(
                node.name
                for node in tree.body
                if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
            )
        )

        definitions_checked += len(defined_names)
        for name in defined_names:
            symbol = getattr(module, name)
            assert symbol.__module__ == legacy_name
            assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol
    assert definitions_checked > 0


def test_legacy_monkeypatch_reaches_each_canonical_image_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for legacy_name, canonical_name in LEGACY_ALIASES.items():
        legacy = importlib.import_module(legacy_name)
        canonical = importlib.import_module(canonical_name)
        sentinel = object()

        monkeypatch.setattr(legacy, "_namespace_migration_probe", sentinel, raising=False)

        assert vars(canonical)["_namespace_migration_probe"] is sentinel


def test_image_schema_and_processing_contracts_remain_stable() -> None:
    route = importlib.import_module(
        "_04_Nucleo_Operativo.capabilities.formats.image.route"
    )
    state = importlib.import_module(
        "_04_Nucleo_Operativo.capabilities.formats.image.state"
    )

    assert state.SCHEMA_VERSION == 5
    config = route.ImageRouteConfig(Path("state") / "image.sqlite3", Path("corpus"))
    assert config.state_path.name == "image.sqlite3"
