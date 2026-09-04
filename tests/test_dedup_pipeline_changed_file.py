"""Regression tests for fail-closed exact duplicate planning."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from neocortex.deduplication import DedupIndex, DedupPlanner, FileChangedError


def test_changed_exact_candidate_is_not_representative_or_redundant(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    keep = corpus / "keep.bin"
    changed = corpus / "changed.bin"
    later = corpus / "later.bin"
    for path, payload in (
        (keep, b"payload-keep!!"),
        (changed, b"payload-change"),
        (later, b"payload-later!"),
    ):
        path.write_bytes(payload)

    # Planning orders this collision set by mtime, so ``changed`` is checked
    # against a stable representative before ``later`` is considered.
    base_ns = 1_700_000_000_000_000_000
    for offset, path in enumerate((keep, changed, later), start=1):
        os.utime(path, ns=(base_ns + (4 - offset) * 1_000_000_000,) * 2)

    database = tmp_path / "state.sqlite3"
    comparisons: list[tuple[str, str]] = []
    digest = bytes.fromhex("ab" * 16)

    def exact_matcher(left, right) -> bool:
        comparisons.append((Path(left.path).name, Path(right.path).name))
        if right.path == str(changed):
            raise FileChangedError("file changed while processing: changed.bin")
        # This deliberately models an adversarial full-hash collision.  If
        # ``changed`` were retained as a representative, ``later`` would be
        # reported as its duplicate even though the comparison never proved
        # that changed file stable.
        return left.path == str(changed)

    with DedupIndex(database) as index:
        scan = index.scan(corpus, excluded_paths=())
        planner = DedupPlanner(index)
        with (
            patch.object(planner, "_fingerprint", return_value=(digest, True)),
            patch(
                "neocortex.deduplication.planning.planner.files_equal_exact",
                side_effect=exact_matcher,
            ),
        ):
            plan = planner.plan(scan.scan_id, preview_limit=None)

    assert comparisons == [("keep.bin", "changed.bin"), ("keep.bin", "later.bin")]
    assert plan.group_count == 0
    assert plan.redundant_files == 0
    assert plan.reclaimable_bytes == 0
    assert plan.statistics.exact_compare_files == 2
    assert plan.statistics.changed_or_unreadable_files == 1
    assert plan.verification_mode == "partial"

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM planned_duplicate_groups"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT group_count,redundant_files,verification_mode "
            "FROM duplicate_plan_summaries WHERE scan_id=?",
            (scan.scan_id,),
        ).fetchone() == (0, 0, "partial")
