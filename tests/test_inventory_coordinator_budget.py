"""Normal inventory preparation must preserve the caller's live work budget."""

from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from neocortex.deduplication import (
    DedupIndex, InventoryExclusionPolicy, InventoryScanBudgetExceeded,
    InventoryScanCancelled, InventoryScanDeadlineExceeded, InventoryWorkBudget,
)
from neocortex.integrations.inventory.inventory_coordinator import prepare_inventory


class State:
    def record_event(self, *args, **kwargs):
        pass

    def referenced_inventory_scan_ids(self):
        return ()

    def update_run_start_cursor(self, *args):
        pass


def prepare(index, root, work_budget=None):
    return prepare_inventory(
        index, State(), 1, root, None, progress=None,
        exclusion_policy=InventoryExclusionPolicy.compile(()),
        publish_portable_checkpoint=True, work_budget=work_budget,
    )


def corpus(tmp_path, count=30):
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(count):
        (root / str(i)).write_bytes(b"item")
    return root


def test_normal_preparation_enforces_item_budget_before_entire_scan(tmp_path: Path):
    root = corpus(tmp_path)
    with DedupIndex(tmp_path / "inventory.sqlite") as index:
        with pytest.raises(InventoryScanBudgetExceeded):
            prepare(index, root, InventoryWorkBudget(max_files=2))
        assert index.inventory_checkpoint(root) is None
        assert index._connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] <= 2


def test_expired_deadline_rejects_before_creating_any_scan(tmp_path: Path):
    root = corpus(tmp_path)
    with DedupIndex(tmp_path / "inventory.sqlite") as index:
        with pytest.raises(InventoryScanDeadlineExceeded):
            prepare(index, root, InventoryWorkBudget(deadline_monotonic=1, monotonic_clock=lambda: 2))
        assert index._connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0


def test_midscan_cancel_preserves_prior_published_generation(tmp_path: Path):
    root = corpus(tmp_path)
    with DedupIndex(tmp_path / "inventory.sqlite") as index:
        original = prepare(index, root)
        previous = index.inventory_checkpoint(root)
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 12

        with pytest.raises(InventoryScanCancelled):
            prepare(index, root, InventoryWorkBudget(cancellation_check=cancelled))
        assert index.inventory_checkpoint(root) == previous
        assert index.scan_summary(original.scan.scan_id) == original.scan
        assert calls < 30
        resumed = prepare(index, root)
        assert resumed.reused_generation


def test_scanner_renews_cpu_io_between_bounded_metadata_batches(tmp_path: Path):
    root = corpus(tmp_path, count=150)
    calls = []

    class Grant:
        def checkpoint(self):
            calls.append("renew")

    class Gate:
        @contextmanager
        def admit(self, estimated_bytes, **kwargs):
            assert estimated_bytes > 0 and kwargs["io_slots"] == 1
            assert kwargs["io_device"] is not None
            assert kwargs["phase"] == "inventory_metadata"
            calls.append("admit")
            try:
                yield Grant()
            finally:
                calls.append("release")

    with DedupIndex(tmp_path / "inventory.sqlite") as index, patch(
        "neocortex.runtime.control.global_resources.resource_gate", return_value=Gate()
    ):
        prepared = prepare(index, root)
    assert prepared.scan.files_seen == 150
    assert calls[0] == "admit" and calls[-1] == "release"
    assert calls.count("renew") >= 3
