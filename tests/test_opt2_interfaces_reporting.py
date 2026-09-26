"""Second-pass contracts for bounded CLI reporting work."""

from __future__ import annotations

import contextlib
import io
from collections import Counter
from types import SimpleNamespace

from neocortex.api.cli import cli_reporting


class _SingleReadMapping(dict[str, object]):
    """Detect accidental duplicate reads of a selected reporting counter."""

    def __init__(self, *args: object, single_read: set[str]) -> None:
        super().__init__(*args)
        self.single_read = single_read
        self.reads: Counter[str] = Counter()

    def __getitem__(self, key: str) -> object:
        self.reads[key] += 1
        if key in self.single_read and self.reads[key] > 1:
            raise AssertionError(f"reporting read {key!r} more than once")
        return super().__getitem__(key)


def test_catalog_reporting_reads_each_optional_counter_once() -> None:
    summary = _SingleReadMapping(
        {
            "candidates": 4,
            "processed": 3,
            "cache_hits": 1,
            "cached_errors": 0,
            "new_work": 3,
            "catalog_complete": True,
            "catalog_candidates": 4,
            "catalog_classified": 3,
            "catalog_cache_hits": 1,
            "catalog_review_required": 0,
            "catalog_errors": 0,
            "catalog_source_stale": 0,
            "catalog_source_missing": 0,
            "catalog_stale_marked": 0,
        },
        single_read={
            "catalog_classified",
            "catalog_cache_hits",
            "catalog_review_required",
            "catalog_stale_marked",
        },
    )
    result = SimpleNamespace(
        route_results={"pdf": summary},
        route_failures={},
        maintenance={},
    )

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        cli_reporting._print_catalog_reports(result)

    assert "ROUTE_CATALOG route=pdf" in output.getvalue()
    assert all(summary.reads[name] == 1 for name in summary.single_read)
