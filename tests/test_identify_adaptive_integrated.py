"""Integrated Identify pilot regressions through a live resource coordinator."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from neocortex.deduplication import DedupIndex
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.control import cpu_runtime
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
    resource_scope,
)
from neocortex.runtime import models as runtime_models
from neocortex.workflow.actions import action_identify
from neocortex.workflow.actions import actions as actions_module
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


def test_integrated_identify_applies_the_same_pilot_after_live_capacity_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    corpus = tmp_path / "corpus"
    state_root = tmp_path / "state"
    corpus.mkdir()
    state_root.mkdir()
    for entry_index in range(32):
        (corpus / f"entry-{entry_index:03d}.txt").write_text(
            f"integrated identify fixture {entry_index}\n", encoding="utf-8"
        )

    # Two small pages expose the post-pilot branch without making a synthetic
    # benchmark look like a million-file run.  The first page is the bounded
    # cold pilot; the next page reuses its completion timings.
    monkeypatch.setattr(action_identify, "TRASH_BATCH_SIZE", 16)
    monkeypatch.setattr(action_identify, "IDENTIFY_PROGRESS_INTERVAL_NS", 0)
    monkeypatch.setattr(cpu_runtime, "effective_cpu_count", lambda: 8)
    original_detector = actions_module.detect_content_type
    active = 0
    maximum_active = 0
    page_maximum_active = [0, 0]
    lock = threading.Lock()

    def detector(path: str):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            page_maximum_active[int(Path(path).stem.rsplit("-", 1)[1]) // 16] = max(
                page_maximum_active[int(Path(path).stem.rsplit("-", 1)[1]) // 16],
                active,
            )
        try:
            # Keep the pilot clearly CPU/GIL-bound rather than manufacturing
            # an I/O wait signal that should widen the adaptive width.
            time.sleep(0.002)
            return original_detector(path)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(actions_module, "detect_content_type", detector)
    coordinator = GlobalResourceCoordinator(
        ("framework",),
        GlobalResourceLimits(
            memory_budget_bytes=64 * 1024 * 1024,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            cpu_slots=8,
            native_thread_slots=8,
            wait_timeout_seconds=1,
            poll_interval_seconds=0.005,
        ),
        effective_cpu_probe=lambda: 8,
        resource_probe=lambda: ResourceSample(
            available_physical=64 * 1024 * 1024,
            available_commit=64 * 1024 * 1024,
            total_physical=128 * 1024 * 1024,
            total_commit=128 * 1024 * 1024,
            cpu_load_percent=0,
            effective_cpu_capacity=8,
        ),
        cpu_load_probe=lambda: 0,
    )
    try:
        with (
            DedupIndex(state_root / "dedup.sqlite3") as dedup_index,
            FrameworkState(state_root / "framework.sqlite3") as state,
            resource_scope(coordinator),
        ):
            scan = dedup_index.scan(corpus)
            run_id = begin_signed_normal_run(state, corpus)
            runner = FrameworkActions(
                dedup_index,
                state,
                run_id,
                scan.scan_id,
                apply=False,
            )
            summary = runner._validate_extensions(
                None,
                runtime_models.ActionSummary(apply_actions=False),
                publish_routes=True,
                prune_cache=False,
            )
    finally:
        # resource_scope owns the coordinator when the context exits; this is
        # only a defensive close for a failure before entering that scope.
        if coordinator._monitor_thread is None:
            coordinator.close()

    assert summary.files_checked == 32
    assert summary.type_cache_misses == 32
    # The first live-width page is the pilot; the GIL-bound signal narrows
    # subsequent pages to one. The scheduler never exceeds the live gate.
    assert maximum_active <= 8
    # The first live-width page is the pilot; only after its observations does
    # the short/GIL-bound signal narrow the integrated submission window.
    assert page_maximum_active[0] <= 8
    assert page_maximum_active[1] <= 1
    route_summary = coordinator.summary().routes["actions.identify"]
    assert route_summary.peak_cpu_slots <= 8
    assert route_summary.cpu_slots_in_use == 0
