"""Contracts for the canonical deduplication namespace after root retirement."""

from __future__ import annotations

import importlib
import os
import pickle
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import neocortex.deduplication as deduplication


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_public_surface_resolves_owned_objects() -> None:
    assert set(deduplication.__all__) <= set(dir(deduplication))
    assert deduplication.FileSnapshot.__module__ == "neocortex.deduplication.domain.models"
    assert deduplication.DedupIndex.__module__ == "neocortex.deduplication.inventory.index"
    assert deduplication.DedupPlanner.__module__ == ("neocortex.deduplication.planning.planner")


def test_canonical_leaf_modules_own_patch_seams() -> None:
    planner = importlib.import_module("neocortex.deduplication.planning.planner")
    pipeline = importlib.import_module("neocortex.deduplication.planning.pipeline")
    sentinel = object()

    with patch("neocortex.deduplication.planning.planner.files_equal_exact", sentinel):
        assert planner.files_equal_exact is sentinel
    with patch(
        "neocortex.deduplication.planning.pipeline.MAX_EXACT_HASH_COLLISION_SETS",
        3,
    ):
        assert pipeline.MAX_EXACT_HASH_COLLISION_SETS == 3


def test_current_pickle_path_resolves_to_the_canonical_type() -> None:
    snapshot = deduplication.FileSnapshot("/tmp/item", 1, 2, 3, 4, 5)

    assert pickle.loads(pickle.dumps(snapshot, protocol=5)) == snapshot
    assert deduplication.FileSnapshot.__module__ == "neocortex.deduplication.domain.models"


def test_numbered_deduplication_root_is_extinct() -> None:
    legacy = "_02" + "_Deduplicacion"

    assert not (REPOSITORY_ROOT / legacy).exists()
    for relative in ("neocortex", "tools"):
        for path in (REPOSITORY_ROOT / relative).rglob("*.py"):
            assert legacy not in path.read_text(encoding="utf-8"), path


def test_canonical_inventory_and_planner_complete_a_small_exact_plan(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "older.bin").write_bytes(b"same-content")
    (corpus / "newer.bin").write_bytes(b"same-content")

    with deduplication.DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(corpus, excluded_paths=())
        plan = deduplication.DedupPlanner(index, partial_threshold=0).plan(
            scan.scan_id,
            preview_limit=None,
        )

    assert plan.group_count == 1
    assert plan.redundant_files == 1


def test_canonical_root_starts_without_loading_heavy_leaves() -> None:
    script = """
import sys
import neocortex.deduplication
assert 'neocortex.deduplication.fingerprinting' not in sys.modules
assert 'neocortex.deduplication.inventory.index' not in sys.modules
assert 'neocortex.deduplication.planning.planner' not in sys.modules
"""
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(REPOSITORY_ROOT),
    }

    completed = subprocess.run(
        (sys.executable, "-B", "-c", script),
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
