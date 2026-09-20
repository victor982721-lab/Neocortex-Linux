"""Focused contracts for the bounded Identify worker pipeline."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from neocortex.deduplication import DedupIndex
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.progress import ProgressEvent
from neocortex.runtime import models as runtime_models
from neocortex.runtime.control import cpu_runtime
from neocortex.workflow.actions import actions as actions_module
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


def _corpus_fixture(tmp_path: Path, count: int = 128) -> tuple[Path, Path]:
    corpus = tmp_path / "corpus"
    state = tmp_path / "state"
    corpus.mkdir()
    state.mkdir()
    for index in range(count):
        (corpus / f"entry-{index:04d}.txt").write_text(
            f"fixture entry {index}\n", encoding="utf-8"
        )
    return corpus, state


def _run_identify(
    index: DedupIndex,
    state: FrameworkState,
    corpus: Path,
    scan_id: int,
    *,
    progress: list[ProgressEvent] | None = None,
) -> runtime_models.ActionSummary:
    run_id = begin_signed_normal_run(state, corpus)
    runner = FrameworkActions(
        index,
        state,
        run_id,
        scan_id,
        apply=False,
        progress=None if progress is None else progress.append,
    )
    return runner._validate_extensions(
        None,
        runtime_models.ActionSummary(apply_actions=False),
        publish_routes=True,
        prune_cache=False,
    )


def test_identify_cold_cache_dispatches_bounded_workers(tmp_path: Path, monkeypatch) -> None:
    corpus, state_root = _corpus_fixture(tmp_path, count=128)
    detector = actions_module.detect_content_type
    worker_ids: set[int] = set()

    def counted_detector(path: str):
        worker_ids.add(threading.get_ident())
        time.sleep(0.002)
        return detector(path)

    monkeypatch.setattr(actions_module, "detect_content_type", counted_detector)
    monkeypatch.setattr(cpu_runtime, "effective_cpu_count", lambda: 8)
    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(state_root / "framework.sqlite3") as state,
    ):
        scan = index.scan(corpus)
        summary = _run_identify(index, state, corpus, scan.scan_id)

    assert summary.type_cache_misses == 128
    assert summary.type_cache_hits == 0
    assert summary.errors == 0
    assert len(worker_ids) >= 2


def test_identify_warm_cache_uses_batch_lookup_and_no_detector(tmp_path: Path, monkeypatch) -> None:
    corpus, state_root = _corpus_fixture(tmp_path, count=128)
    detector = actions_module.detect_content_type
    detector_calls = 0
    batch_calls = 0
    original_batch = FrameworkState.get_content_type_cache_batch

    def counted_detector(path: str):
        nonlocal detector_calls
        detector_calls += 1
        return detector(path)

    def counted_batch(self: FrameworkState, snapshots, version):
        nonlocal batch_calls
        batch_calls += 1
        return original_batch(self, snapshots, version)

    monkeypatch.setattr(actions_module, "detect_content_type", counted_detector)
    monkeypatch.setattr(FrameworkState, "get_content_type_cache_batch", counted_batch)
    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(state_root / "framework.sqlite3") as state,
    ):
        scan = index.scan(corpus)
        cold = _run_identify(index, state, corpus, scan.scan_id)
        warm = _run_identify(index, state, corpus, scan.scan_id)

    assert cold.type_cache_misses == 128
    assert warm.type_cache_hits == 128
    assert warm.type_cache_misses == 0
    assert detector_calls == 128
    assert batch_calls == 2


def test_identify_progress_is_coalesced_and_ordered(tmp_path: Path) -> None:
    corpus, state_root = _corpus_fixture(tmp_path, count=256)
    events: list[ProgressEvent] = []
    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(state_root / "framework.sqlite3") as state,
    ):
        scan = index.scan(corpus)
        summary = _run_identify(index, state, corpus, scan.scan_id, progress=events)

    assert summary.files_checked == 256
    assert len(events) < 64
    assert events[0].completed == 0
    assert events[-1].finished is True
    completed = [event.completed for event in events]
    assert completed == sorted(completed)
