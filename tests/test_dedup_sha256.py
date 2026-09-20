"""Acceptance contract for the single SHA-256 deduplication pipeline."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

from neocortex.deduplication import DedupIndex, DedupPlanner, FULL_ALGORITHM, snapshot_path
from neocortex.deduplication import fingerprinting


def test_full_digest_is_the_complete_stdlib_sha256_vector(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    payload = b"neocortex-sha256-acceptance\0" * 10
    source.write_bytes(payload)

    assert FULL_ALGORITHM == "sha256_full_v1"
    assert fingerprinting.full_fingerprint(snapshot_path(source)) == hashlib.sha256(payload).digest()


def test_size_buckets_hash_only_repeated_sizes_and_require_exact_bytes(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "unique").write_bytes(b"u")
    (root / "same-a").write_bytes(b"same")
    (root / "same-b").write_bytes(b"same")
    (root / "collision").write_bytes(b"diff")

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        with patch(
            "neocortex.deduplication.planning.planner.files_equal_exact",
            wraps=fingerprinting.files_equal_exact,
        ) as exact:
            plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)

    assert plan.group_count == 1
    assert plan.statistics.size_candidate_files == 3
    assert plan.statistics.partial_hash_files == 0
    assert plan.statistics.full_hash_files == 3
    assert plan.statistics.exact_compare_files == 1
    assert exact.call_count == 1
    assert plan.groups[0].full_fingerprint == hashlib.sha256(b"same").hexdigest()
    assert plan.groups[0].proof is not None
    assert plan.groups[0].proof.comparison_method == "byte_for_byte"


def test_sha_match_without_exact_policy_is_explicitly_non_authoritative(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").write_bytes(b"same")
    (root / "b").write_bytes(b"same")

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=False, preview_limit=None)

    assert plan.group_count == 1
    group = plan.groups[0]
    assert group.proof is not None
    assert group.proof.comparison_method == "sha256_full"
    assert group.proof.comparison_result == "fingerprint_match"
    assert "byte_for_byte_comparison" in group.proof.missing_checks


def test_source_drift_abstains_before_publishing_a_duplicate_group(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    first = root / "a"
    second = root / "b"
    first.write_bytes(b"same")
    second.write_bytes(b"same")

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        planner = DedupPlanner(index)
        original = planner._fingerprint
        drifted = False

        def fingerprint(snapshot):
            nonlocal drifted
            observation = original(snapshot)
            if not drifted and Path(snapshot.path).name == "a":
                first.write_bytes(b"drift")
                drifted = True
            return observation

        with patch.object(planner, "_fingerprint", side_effect=fingerprint):
            plan = planner.plan(scan.scan_id, preview_limit=None)

    assert plan.group_count == 0
    assert plan.coverage == "partial"
    assert plan.statistics.changed_or_unreadable_files >= 1


def test_full_hash_cache_is_revalidated_and_content_change_invalidates_it(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    first = root / "a"
    second = root / "b"
    first.write_bytes(b"same")
    second.write_bytes(b"same")

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        cold = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)
        warm = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)
        first.write_bytes(b"diff")
        changed_scan = index.scan(root, excluded_paths=())
        changed = DedupPlanner(index).plan(changed_scan.scan_id, preview_limit=None)

    assert cold.statistics.fingerprint_cache_hits == 0
    assert warm.statistics.fingerprint_cache_hits == 2
    assert warm.statistics.cache_validation_reads == 2
    assert changed.statistics.fingerprint_cache_hits == 1
    assert changed.group_count == 0


def test_owned_hash_surface_has_no_removed_dual_backend_or_partial_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [root / "neocortex" / "foundation" / "hash_compat.py"]
    paths.extend((root / "neocortex" / "deduplication").rglob("*.py"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "xxhash" not in source
    assert "XXH3" not in source
    assert "sha256_128_fallback" not in source
    assert "partial_fingerprint" not in source
