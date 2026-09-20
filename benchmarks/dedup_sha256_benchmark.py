#!/usr/bin/env python3
"""Reproducible local benchmark for the canonical SHA-256 dedupe path.

The default run creates a temporary fixture only.  It reports the size-bucket
candidate count, complete SHA-256 bytes read, wall time, and a second-run
cache replay without touching a personal corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

# Running the file directly puts ``benchmarks/`` first on ``sys.path``.  Make
# the source under test explicit so the benchmark cannot silently exercise an
# installed release or another checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from neocortex.deduplication import DedupIndex, DedupPlanner  # noqa: E402


def _fixture(root: Path, *, unique_files: int, duplicate_groups: int, size: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for number in range(unique_files):
        (root / f"unique-{number:03d}").write_bytes(bytes([number % 251]) * (size + number + 1))
    for number in range(duplicate_groups):
        payload = (f"duplicate-{number:03d}\0".encode() * ((size // 16) + 1))[:size]
        (root / f"duplicate-{number:03d}-a").write_bytes(payload)
        (root / f"duplicate-{number:03d}-b").write_bytes(payload)


def _run(index: DedupIndex, root: Path) -> tuple[object, float]:
    started = time.perf_counter()
    scan = index.scan(root, excluded_paths=())
    plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)
    return plan, time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unique-files", type=int, default=8)
    parser.add_argument("--duplicate-groups", type=int, default=8)
    parser.add_argument("--size", type=int, default=1024 * 1024)
    args = parser.parse_args()
    if min(args.unique_files, args.duplicate_groups, args.size) < 0 or args.size == 0:
        parser.error("fixture dimensions must be non-negative and --size must be positive")

    with tempfile.TemporaryDirectory(prefix="neocortex-dedup-benchmark-") as directory:
        base = Path(directory)
        root = base / "corpus"
        _fixture(
            root,
            unique_files=args.unique_files,
            duplicate_groups=args.duplicate_groups,
            size=args.size,
        )
        with DedupIndex(base / "inventory.sqlite3") as index:
            cold, cold_seconds = _run(index, root)
            warm, warm_seconds = _run(index, root)
        cold_stats = cold.statistics
        warm_stats = warm.statistics
        payload = {
            "algorithm": "sha256_full_v1",
            "files": cold_stats.inventory_files,
            "bytes": sum(path.stat().st_size for path in root.iterdir()),
            "candidates_by_size": cold_stats.size_candidate_files,
            "cold": {
                "wall_seconds": cold_seconds,
                "bytes_hashed": cold_stats.hash_read_bytes,
                "full_hash_files": cold_stats.full_hash_files,
                "groups": cold.group_count,
                "cache_hits": cold_stats.fingerprint_cache_hits,
            },
            "replay": {
                "wall_seconds": warm_seconds,
                "bytes_hashed": warm_stats.hash_read_bytes,
                "full_hash_files": warm_stats.full_hash_files,
                "groups": warm.group_count,
                "cache_hits": warm_stats.fingerprint_cache_hits,
            },
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
