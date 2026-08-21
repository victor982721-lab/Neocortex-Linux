"""Architecture contracts for the modular deduplication inventory repository."""

from __future__ import annotations

import ast
from pathlib import Path

from neocortex.deduplication.inventory import index as index_module
from neocortex.deduplication.inventory.index import DedupIndex


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = REPOSITORY_ROOT / "neocortex/deduplication/inventory/index.py"


def test_index_is_an_explicit_low_complexity_facade() -> None:
    source = INDEX_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    facade = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DedupIndex"
    )

    assert len(source.splitlines()) < 175
    assert not any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert {
        node.name
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } == {"__init__", "scan", "size_collision_groups", "close", "__enter__", "__exit__"}


def test_dedup_index_composes_responsibility_owned_methods() -> None:
    expected_owners = {
        "inventory_checkpoint": "repository_scans",
        "prune_obsolete_state": "repository_scans",
        "snapshots": "repository_files",
        "store_fingerprints": "repository_files",
        "begin_planning_fingerprints": "repository_plans",
        "iter_duplicate_groups": "repository_plans",
        "apply_reconciliation": "repository_reconciliation",
    }

    assert DedupIndex.__module__ == "neocortex.deduplication.inventory.index"
    for method_name, owner in expected_owners.items():
        assert getattr(DedupIndex, method_name).__module__.endswith(owner)


def test_constructor_and_file_refresh_patch_seams_remain_live(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialized: list[Path] = []
    original_initialize = index_module.initialize_inventory_schema

    def initialize(path: str | Path) -> None:
        initialized.append(Path(path))
        original_initialize(path)

    monkeypatch.setattr(index_module, "initialize_inventory_schema", initialize)
    with DedupIndex(tmp_path / "inventory.sqlite3") as repository:
        assert repository.path == tmp_path / "inventory.sqlite3"

        scan_result = object()
        scanner_connections: list[object] = []

        class Scanner:
            def __init__(self, connection) -> None:
                scanner_connections.append(connection)

            def scan(self, _root, **_options):
                return scan_result

        monkeypatch.setattr(index_module, "InventoryScanner", Scanner)
        assert repository.scan(tmp_path) is scan_result
        assert scanner_connections == [repository._connection]

        sentinel_resolver = object()
        observed: list[object] = []

        def collision_groups(_connection, _scan_id, *, snapshot_resolver):
            observed.append(snapshot_resolver)
            return iter(())

        monkeypatch.setattr(index_module, "snapshot_path", sentinel_resolver)
        monkeypatch.setattr(index_module, "iter_size_collision_groups", collision_groups)

        assert tuple(repository.size_collision_groups(7)) == ()
        assert observed == [sentinel_resolver]

    assert initialized == [tmp_path / "inventory.sqlite3"]
