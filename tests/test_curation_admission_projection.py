"""The admission gate filters observations, never rewrites dedupe policy."""

from pathlib import Path

from neocortex.deduplication import DedupIndex, DedupPlanner, snapshot_path
from neocortex.deduplication.inventory.curation_admission import CurationAdmission


def test_delta_does_not_reopen_stable_population_and_has_no_256_child_cutoff(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    for number in range(301):
        (root / f"original-{number}.txt").write_text(str(number))
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        first = index.scan(root)
        with CurationAdmission(index, checkpoint=lambda: None) as admission:
            assert admission.capture_delta(first.scan_id) == 301
            admission.settle(lambda _snapshot: (True, "curated"))
            for number in range(301):
                (root / f"child-{number}.txt").write_text(str(number))
            second = index.scan(root)
            assert admission.capture_delta(second.scan_id) == 301
            assert all(Path(item.path).name.startswith("child-") for item in admission.snapshots())
            admission.settle(lambda _snapshot: (True, "curated"))
            assert admission.capture_delta(second.scan_id) == 0
            assert admission.summary()["checked"] == 602
        assert not admission.permits(snapshot_path(root / "original-1.txt"))


def test_gate_filters_before_stat_or_hash_and_preserves_exact_duplicates(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    for name in ("keep.txt", "duplicate.txt", "junk.exe"):
        (root / name).write_bytes(b"identical bytes")
    import neocortex.deduplication.planning.planner as planner_module
    captured = []
    real_capture = planner_module.snapshot_path

    def capture(path):
        captured.append(Path(path).name)
        assert Path(path).name != "junk.exe"
        return real_capture(path)

    monkeypatch.setattr(planner_module, "snapshot_path", capture)
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(root)
        with CurationAdmission(index, checkpoint=lambda: None) as admission:
            admission.capture_delta(scan.scan_id)
            admission.settle(lambda item: (not item.path.endswith(".exe"), "policy"))
            plan = DedupPlanner(index, admission_check=admission.permits).plan(scan.scan_id)
            assert plan.group_count == 1
            assert plan.redundant_files == 1
            assert set(captured) == {"keep.txt", "duplicate.txt"}


def test_only_one_survivor_in_raw_size_bucket_requires_no_fingerprint(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "keep.txt").write_bytes(b"1234")
    (root / "junk.exe").write_bytes(b"abcd")
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(root)
        with CurationAdmission(index, checkpoint=lambda: None) as admission:
            admission.capture_delta(scan.scan_id)
            admission.settle(lambda item: (item.path.endswith("keep.txt"), "policy"))
            planner = DedupPlanner(index, admission_check=admission.permits)
            def forbidden(_snapshot):
                raise AssertionError("a singleton survivor must not be hashed")
            monkeypatch.setattr(planner, "_fingerprint", forbidden)
            assert planner.plan(scan.scan_id).group_count == 0


def test_rename_rebinding_and_changed_observation_reenter_gate(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    old = root / "report.dat"
    old.write_bytes(b"first")
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(root)
        with CurationAdmission(index, checkpoint=lambda: None) as admission:
            admission.capture_delta(scan.scan_id)
            new = old.with_suffix(".pdf")
            old.rename(new)
            index.apply_reconciliation(scan.scan_id, upserts=(snapshot_path(new),), remove_paths=(str(old),))
            admission.rebind({str(old): str(new)})
            assert [item.path for item in admission.current_snapshots()] == [str(new)]
            admission.settle(lambda _snapshot: (True, "curated"))
            assert admission.capture_delta(scan.scan_id) == 0
            assert admission.permits(snapshot_path(new))
            new.write_bytes(b"changed payload")
            latest = index.scan(root)
            assert admission.capture_delta(latest.scan_id) == 1
            assert not admission.permits(snapshot_path(new))


def test_failed_normalization_intent_does_not_erase_pending_observation(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "report.dat"
    source.write_bytes(b"unmoved content")
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(root)
        with CurationAdmission(index, checkpoint=lambda: None) as admission:
            admission.capture_delta(scan.scan_id)
            admission.rebind({str(source): str(source.with_suffix('.pdf'))})
            assert [item.path for item in admission.current_snapshots()] == [str(source)]
            admission.settle(lambda _snapshot: (False, "normalization_incomplete"))
            assert not admission.permits(snapshot_path(source))
            assert admission.capture_delta(scan.scan_id) == 0
