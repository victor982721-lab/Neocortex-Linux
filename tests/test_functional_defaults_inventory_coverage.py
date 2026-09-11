"""Default dry-run coverage keeps every physical source observable."""

from __future__ import annotations

import hashlib
from pathlib import Path

from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


def _run_action_dry_run(tmp_path: Path):
    corpus = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    corpus.mkdir()
    state_directory.mkdir()

    (corpus / "keeper.txt").write_bytes(b"same physical content\n")
    (corpus / "duplicate.txt").write_bytes(b"same physical content\n")
    (corpus / "image.bin").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    (corpus / "empty.bin").touch()

    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in corpus.iterdir()
    }
    with (
        DedupIndex(tmp_path / "dedup.sqlite3") as index,
        FrameworkState(state_directory / "framework.sqlite3") as state,
    ):
        scan = index.scan(corpus)
        plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=True, preview_limit=None)
        run_id = begin_signed_normal_run(state, corpus)
        summary = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=False,
        ).execute(plan)
        route_rows = state._connection.execute(
            """SELECT mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            FROM route_candidates WHERE run_id=? ORDER BY path""",
            (run_id,),
        ).fetchall()
        action_rows = state._connection.execute(
            """SELECT action_type,source_path,status,detail
            FROM file_actions WHERE run_id=? ORDER BY action_id""",
            (run_id,),
        ).fetchall()
        inventory = {
            item.path: item
            for item in index.snapshots(scan.scan_id)
        }

    return corpus, before, summary, route_rows, action_rows, inventory


def test_dry_run_retains_duplicate_identity_in_route_coverage(tmp_path: Path) -> None:
    corpus, before, summary, route_rows, action_rows, inventory = _run_action_dry_run(tmp_path)

    # A proposal is not an effect: both members remain route inputs and keep
    # their independently observed physical identities.
    assert {row[1] for row in route_rows} == {
        str(corpus / "keeper.txt"),
        str(corpus / "duplicate.txt"),
        str(corpus / "image.bin"),
    }
    for _mime, path, volume_id, file_id, size, mtime_ns, birthtime_ns in route_rows:
        recorded = inventory[path]
        assert (int(volume_id, 16), int(file_id, 16)) == recorded.identity
        assert (size, mtime_ns, birthtime_ns) == (
            recorded.size,
            recorded.mtime_ns,
            recorded.birthtime_ns,
        )

    assert summary.duplicate_candidates == 2  # one empty file plus one redundant member
    assert summary.duplicates_trashed == 0
    assert summary.files_checked == 3
    assert summary.types_detected == 3
    assert summary.unknown_types == 0
    assert {row[0:3] for row in action_rows} == {
        ("trash_empty_file", str(corpus / "empty.bin"), "planned"),
        ("trash_duplicate", str(corpus / "duplicate.txt"), "planned"),
        ("correct_extension", str(corpus / "image.bin"), "planned"),
    }
    assert all(row[3] is None for row in action_rows)
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in corpus.iterdir()
    } == before


def test_empty_file_is_an_explicit_planned_exclusion_not_success(tmp_path: Path) -> None:
    corpus, _before, summary, route_rows, action_rows, _inventory = _run_action_dry_run(tmp_path)

    empty = str(corpus / "empty.bin")
    assert empty not in {row[1] for row in route_rows}
    empty_action = next(row for row in action_rows if row[1] == empty)
    assert empty_action[0:3] == ("trash_empty_file", empty, "planned")
    assert summary.duplicates_trashed == 0
    assert (corpus / "empty.bin").exists()
