"""Contracts for the cohesive, product-only Code namespace."""

from __future__ import annotations

import importlib
import sqlite3
import subprocess
import sys
from pathlib import Path

from neocortex.code.code_schema import initialize_code_state, validate_code_schema


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_ROOT = PROJECT_ROOT / "neocortex" / "code"


def test_product_code_modules_are_exposed_under_cohesive_subpackages() -> None:
    modules = (
        ("code_contracts", "neocortex.code.code_contracts"),
        ("code_route", "neocortex.code.code_route"),
        ("code_schema", "neocortex.code.code_schema"),
        ("code_search", "neocortex.code.search.code_search"),
        ("ingestion", "neocortex.code.ingestion"),
        ("search", "neocortex.code.search"),
    )
    for _name, canonical_name in modules:
        product = importlib.import_module(canonical_name)
        assert sys.modules[canonical_name] is product
        assert Path(product.__file__).resolve().is_relative_to(CODE_ROOT)

    assert not (CODE_ROOT / "contracts" / "target_registry.py").exists()
    assert not (CODE_ROOT / "contracts" / "target_projection.py").exists()


def test_code_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.code")
forbidden = {
    "neocortex.code.code_contracts",
    "neocortex.code.code_route",
    "neocortex.code.ingestion",
    "neocortex.code.search",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("code loaded eagerly: " + ",".join(loaded))
print("CODE_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "CODE_IMPORT_LIGHT"


def test_fresh_code_state_contains_product_tables_only(tmp_path: Path) -> None:
    database = tmp_path / "code.sqlite3"
    initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        validate_code_schema(connection)
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    assert "analysis_runs" in names
    assert "code_chunks" in names
    assert not any(name.startswith("external_") for name in names)
    assert "code_experiment_receipts" not in names
