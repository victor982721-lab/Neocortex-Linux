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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = "neocortex.capabilities.formats.image"

IMAGE_MODULES = (
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
def test_image_implementation_lives_under_the_product_namespace() -> None:
    product_root = PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "image"
    assert not (product_root / "adult.py").exists()
    for name in IMAGE_MODULES:
        module = importlib.import_module(f"{PRODUCT_ROOT}.{name}")
        assert Path(module.__file__).resolve().is_relative_to(product_root)

    for relative_path in (
        "neocortex/api/public.py",
        "neocortex/runtime/config/application_config_projections.py",
        "neocortex/api/cli/cli_video.py",
        "neocortex/knowledge/knowledge_snapshot.py",
        "neocortex/runtime/models.py",
        "neocortex/runtime/orchestration/orchestrator.py",
        "neocortex/runtime/orchestration/route_registry.py",
        "neocortex/semantic/semantic_plan_owners.py",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert PRODUCT_ROOT in source


def test_image_parent_packages_remain_import_light() -> None:
    script = textwrap.dedent(
        """
        import sys

        import neocortex.capabilities
        import neocortex.capabilities.formats
        import neocortex.capabilities.formats.image

        forbidden = set()
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


def test_canonical_image_modules_do_not_import_outside_their_tree() -> None:
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        for module_name in {tuple(f"{PRODUCT_ROOT}.{name}" for name in IMAGE_MODULES)!r}:
            importlib.import_module(module_name)
        loaded = sorted(
            name
            for name in sys.modules
            if name == "_04_Nucleo_Operativo"
            or name.startswith("_04_Nucleo_Operativo.")
        )
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


def test_image_definitions_are_owned_by_canonical_modules() -> None:
    definitions_checked = 0
    for name in IMAGE_MODULES:
        module = importlib.import_module(f"{PRODUCT_ROOT}.{name}")
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
            assert symbol.__module__ == module.__name__
            assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol
    assert definitions_checked > 0


def test_image_schema_and_processing_contracts_remain_stable() -> None:
    route = importlib.import_module(
        "neocortex.capabilities.formats.image.route"
    )
    state = importlib.import_module(
        "neocortex.capabilities.formats.image.state"
    )

    assert state.SCHEMA_VERSION == 6
    config = route.ImageRouteConfig(Path("state") / "image.sqlite3", Path("corpus"))
    assert config.state_path.name == "image.sqlite3"
