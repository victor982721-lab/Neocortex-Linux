"""D2-CATALOG avoids repeating the inventory size grouping query."""

from __future__ import annotations

from pathlib import Path

from neocortex.deduplication import DedupIndex, DedupPlanner


def test_planner_spills_size_buckets_once_and_preserves_candidate_evidence(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for number in range(24):
        size = 128 + (number % 3) * 17
        (corpus / f"duplicate-{number:02d}.bin").write_bytes(
            bytes([65 + number % 3]) * size
        )
    for number in range(6):
        (corpus / f"unique-{number:02d}.bin").write_bytes(bytes([number]) * (1000 + number))

    statements: list[str] = []
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(corpus, excluded_paths=())
        index._connection.set_trace_callback(statements.append)
        try:
            plan = DedupPlanner(index).plan(
                scan.scan_id,
                exact_compare=False,
                preview_limit=None,
            )
        finally:
            index._connection.set_trace_callback(None)

        grouping = [
            statement
            for statement in statements
            if "GROUP BY size" in statement and "files" in statement
        ]
        assert len(grouping) == 1

    assert plan.group_count == 3
    assert plan.statistics.size_candidate_files == 24
    assert plan.statistics.full_hash_files == 24
    assert plan.statistics.changed_or_unreadable_files == 0
    assert plan.coverage == "complete"
