"""Regression coverage for the bounded, completion-driven Identify scheduler."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import pytest

from neocortex.deduplication import DedupIndex
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.progress import ProgressEvent
from neocortex.runtime import models as runtime_models
from neocortex.runtime.control import cpu_runtime
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.workflow.actions import action_identify
from neocortex.workflow.actions import actions as actions_module
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


def _fixture(tmp_path: Path, count: int = 80) -> tuple[Path, Path]:
    corpus = tmp_path / "corpus"
    state = tmp_path / "state"
    corpus.mkdir()
    state.mkdir()
    for index in range(count):
        (corpus / f"entry-{index:03d}.txt").write_text(
            f"fixture entry {index}\n", encoding="utf-8"
        )
    return corpus, state


def _run(
    corpus: Path,
    state_root: Path,
    *,
    progress: list[ProgressEvent] | None = None,
    cancellation_check=None,
):
    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(state_root / "framework.sqlite3") as state,
    ):
        scan = index.scan(corpus)
        run_id = begin_signed_normal_run(state, corpus)
        runner = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=False,
            progress=None if progress is None else progress.append,
            cancellation_check=cancellation_check,
        )
        summary = runner._validate_extensions(
            None,
            runtime_models.ActionSummary(apply_actions=False),
            publish_routes=True,
            prune_cache=False,
        )
        rows = state._connection.execute(
            "SELECT path FROM route_candidates WHERE run_id=? ORDER BY path",
            (run_id,),
        ).fetchall()
    return summary, [row[0] for row in rows]


@pytest.mark.parametrize("slow_index", (0, 40, 79))
def test_slow_observation_does_not_hide_siblings_and_order_stays_deterministic(
    tmp_path: Path,
    monkeypatch,
    slow_index: int,
) -> None:
    """A slow early, middle, or final item must not cause HOL progress stalls."""

    corpus, state_root = _fixture(tmp_path)
    slow_path = corpus / f"entry-{slow_index:03d}.txt"
    started = threading.Event()
    release = threading.Event()
    progress: list[ProgressEvent] = []
    watcher_errors: list[str] = []
    original_detector = actions_module.detect_content_type

    def detector(path: str):
        if path == str(slow_path):
            started.set()
            release.wait(3)
        return original_detector(path)

    monkeypatch.setattr(actions_module, "detect_content_type", detector)
    monkeypatch.setattr(cpu_runtime, "effective_cpu_count", lambda: 8)
    monkeypatch.setattr(action_identify, "IDENTIFY_PROGRESS_ITEM_STEP", 1)
    monkeypatch.setattr(action_identify, "IDENTIFY_PROGRESS_INTERVAL_NS", 0)

    def observe_progress_while_slow_is_held() -> None:
        if not started.wait(2):
            watcher_errors.append("slow detector did not start")
            release.set()
            return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if any(event.completed > 0 for event in progress):
                release.set()
                return
            time.sleep(0.005)
        watcher_errors.append("no sibling observation completed while slow item was held")
        release.set()

    watcher = threading.Thread(target=observe_progress_while_slow_is_held)
    watcher.start()
    try:
        summary, paths = _run(corpus, state_root, progress=progress)
    finally:
        release.set()
        watcher.join(3)

    assert not watcher_errors
    assert summary.files_checked == 80
    assert paths == [str(corpus / f"entry-{index:03d}.txt") for index in range(80)]
    assert [event.completed for event in progress] == sorted(
        event.completed for event in progress
    )


def test_identify_keeps_one_future_per_bounded_window(tmp_path: Path, monkeypatch) -> None:
    corpus, state_root = _fixture(tmp_path, count=96)
    original_detector = actions_module.detect_content_type
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def detector(path: str):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.003)
            return original_detector(path)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(actions_module, "detect_content_type", detector)
    monkeypatch.setattr(cpu_runtime, "effective_cpu_count", lambda: 8)

    real_executor = concurrent.futures.ThreadPoolExecutor
    maximum_pending = 0
    pending = 0

    class TrackingExecutor(real_executor):
        def submit(self, *args, **kwargs):
            nonlocal maximum_pending, pending
            with lock:
                pending += 1
                maximum_pending = max(maximum_pending, pending)
            future = super().submit(*args, **kwargs)

            def finished(_future) -> None:
                nonlocal pending
                with lock:
                    pending -= 1

            future.add_done_callback(finished)
            return future

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", TrackingExecutor)
    summary, _ = _run(corpus, state_root)

    # The direct-capacity pilot is 8 // 4 == 2.  Both execution and queued
    # futures remain within that same bounded window.
    assert maximum_active <= 2
    assert maximum_pending <= 2
    assert summary.type_cache_misses == 96


def test_identify_persists_only_on_caller_thread(tmp_path: Path, monkeypatch) -> None:
    corpus, state_root = _fixture(tmp_path, count=48)
    owner_thread = threading.get_ident()
    calls: list[tuple[str, int]] = []

    original_batch = FrameworkState.get_content_type_cache_batch
    original_store = FrameworkState.store_content_type_cache_batch

    def batch(self, *args, **kwargs):
        calls.append(("lookup", threading.get_ident()))
        return original_batch(self, *args, **kwargs)

    def store(self, *args, **kwargs):
        calls.append(("store", threading.get_ident()))
        return original_store(self, *args, **kwargs)

    monkeypatch.setattr(FrameworkState, "get_content_type_cache_batch", batch)
    monkeypatch.setattr(FrameworkState, "store_content_type_cache_batch", store)
    monkeypatch.setattr(cpu_runtime, "effective_cpu_count", lambda: 8)

    _run(corpus, state_root)

    assert calls
    assert {thread_id for _, thread_id in calls} == {owner_thread}


def test_identify_cancellation_does_not_wait_for_running_detector(
    tmp_path: Path,
    monkeypatch,
) -> None:
    corpus, state_root = _fixture(tmp_path, count=64)
    slow_started = threading.Event()
    release = threading.Event()
    workers_done = threading.Event()
    active = 0
    lock = threading.Lock()
    original_detector = actions_module.detect_content_type

    def detector(path: str):
        nonlocal active
        with lock:
            active += 1
        slow_started.set()
        try:
            release.wait(5)
            return original_detector(path)
        finally:
            with lock:
                active -= 1
                if active == 0:
                    workers_done.set()

    monkeypatch.setattr(actions_module, "detect_content_type", detector)
    monkeypatch.setattr(cpu_runtime, "effective_cpu_count", lambda: 8)

    cancelled = threading.Event()

    def cancellation_check() -> None:
        if cancelled.is_set():
            raise CancellationRequested("test cancellation")

    def cancel_after_worker_starts() -> None:
        assert slow_started.wait(2)
        cancelled.set()

    canceller = threading.Thread(target=cancel_after_worker_starts)
    canceller.start()
    started_at = time.monotonic()
    try:
        with pytest.raises(CancellationRequested):
            _run(
                corpus,
                state_root,
                cancellation_check=cancellation_check,
            )
    finally:
        elapsed = time.monotonic() - started_at
        release.set()
        canceller.join(3)
        assert workers_done.wait(3)

    assert elapsed < 1.0
