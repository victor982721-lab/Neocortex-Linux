# region [00] Contexto del módulo
# Módulo: tests/test_dedup_pipeline_modularity.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import os
import sqlite3
from dataclasses import astuple
from pathlib import Path
from unittest.mock import patch

import pytest

from _02_Deduplicacion import DedupIndex, DedupPlanner, ScanSummary
from _03_Progreso import RecordingProgress
# endregion [01]

# region [02] Implementación


def _summary_without_identifier(summary: ScanSummary) -> tuple[object, ...]:
    return astuple(summary)[1:]


def test_inventory_batch_boundaries_preserve_scan_and_progress_parity(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    excluded = corpus / "excluded"
    nested = corpus / "nested" / "deep"
    excluded.mkdir(parents=True)
    nested.mkdir(parents=True)
    (corpus / "root.bin").write_bytes(b"root")
    (nested / "first.bin").write_bytes(b"first")
    (nested / "second.bin").write_bytes(b"second")
    (excluded / "ignored.bin").write_bytes(b"ignored")

    results = []
    for ordinal, batch_size in enumerate((1, 64), start=1):
        progress = RecordingProgress()
        with DedupIndex(tmp_path / f"state-{ordinal}.sqlite3") as index:
            summary = index.scan(
                corpus,
                batch_size=batch_size,
                excluded_paths=(excluded,),
                progress=progress,
            )
            snapshots = tuple(index.snapshots(summary.scan_id))
        results.append((summary, snapshots, tuple(progress.events)))

    first_summary, first_snapshots, first_events = results[0]
    second_summary, second_snapshots, second_events = results[1]
    assert _summary_without_identifier(first_summary) == _summary_without_identifier(second_summary)
    assert first_snapshots == second_snapshots
    assert first_summary.files_seen == 3
    assert first_summary.excluded_directories == 1
    for events in (first_events, second_events):
        assert events[0].description == "Inventariando archivos"
        assert events[0].completed == 0
        assert events[-1].description == "Inventario completado"
        assert events[-1].completed == events[-1].total == 3
        assert events[-1].finished


def test_inventory_cancellation_flushes_a_durable_partial_checkpoint(
    tmp_path: Path,
) -> None:
    class StopScan(Exception):
        pass

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for position in range(512):
        (corpus / f"item-{position:04d}.bin").touch()

    def stop_at_batch_boundary(event: object) -> None:
        if getattr(event, "completed", None) == 512:
            raise StopScan

    database = tmp_path / "state.sqlite3"
    with pytest.raises(StopScan):
        with DedupIndex(database) as index:
            index.scan(
                corpus,
                batch_size=512,
                excluded_paths=(),
                progress=stop_at_batch_boundary,
            )

    with sqlite3.connect(database) as connection:
        scan = connection.execute(
            "SELECT completed_ns,status FROM scans ORDER BY scan_id DESC LIMIT 1"
        ).fetchone()
        retained_rows = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert scan is not None
    assert isinstance(scan[0], int)
    assert scan[1] == "partial"
    assert retained_rows == 512


def test_exact_verification_separates_adversarial_full_hash_collisions(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    payloads = {
        "newest-duplicate.bin": b"duplicate-content",
        "older-duplicate.bin": b"duplicate-content",
        "collision-one.bin": b"collision-content",
        "collision-two.bin": b"different-content",
    }
    for order, (name, payload) in enumerate(payloads.items(), start=1):
        path = corpus / name
        path.write_bytes(payload)
        timestamp = 1_700_000_000_000_000_000 + (5 - order) * 1_000_000_000
        os.utime(path, ns=(timestamp, timestamp))

    with DedupIndex(tmp_path / "state.sqlite3") as index:
        scan = index.scan(corpus, excluded_paths=())
        planner = DedupPlanner(index)
        with patch.object(
            planner,
            "_fingerprint",
            return_value=(bytes.fromhex("00" * 16), True),
        ):
            plan = planner.plan(scan.scan_id, preview_limit=None)

    assert plan.group_count == 1
    assert plan.statistics.full_hash_files == 4
    assert plan.statistics.changed_or_unreadable_files == 0
    assert Path(plan.groups[0].keep.path).name == "newest-duplicate.bin"
    assert [Path(item.path).name for item in plan.groups[0].redundant] == ["older-duplicate.bin"]


def test_exact_collision_set_limit_abstains_instead_of_merging_distinct_files(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for position, payload in enumerate((b"aaaa", b"bbbb", b"cccc", b"dddd")):
        path = corpus / f"distinct-{position}.bin"
        path.write_bytes(payload)
        timestamp = 1_700_000_000_000_000_000 + position * 1_000_000_000
        os.utime(path, ns=(timestamp, timestamp))

    with DedupIndex(tmp_path / "state.sqlite3") as index:
        scan = index.scan(corpus, excluded_paths=())
        planner = DedupPlanner(index)
        with (
            patch.object(
                planner,
                "_fingerprint",
                return_value=(bytes.fromhex("ff" * 16), True),
            ),
            patch(
                "_02_Deduplicacion.planning_pipeline.MAX_EXACT_HASH_COLLISION_SETS",
                2,
            ),
        ):
            plan = planner.plan(scan.scan_id, preview_limit=None)

    assert plan.group_count == 0
    assert plan.redundant_files == 0
    assert plan.statistics.size_candidate_files == 4
    assert plan.statistics.full_hash_files == 4
    assert plan.statistics.exact_compare_files == 5
    assert plan.statistics.changed_or_unreadable_files == 2


# endregion [02]
