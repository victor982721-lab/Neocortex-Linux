"""Fatal Image shutdown must stop sibling work before joining the executor."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from neocortex.capabilities.formats.image.isolation import ImageWorkerSupervisor
from neocortex.capabilities.formats.image.route import ImageRoute, ImageRouteConfig
from neocortex.capabilities.formats.image.state import iter_candidates
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
)
from neocortex.runtime.control.memory_runtime import (
    MemoryHeadroomTimeout,
    MemoryResourceLimits,
    WeightedMemoryGate,
)


TEST_CAPABILITIES = ("image",)
MIB = 1024 * 1024


class _State:
    def __init__(self, paths):
        self.rows = [("image/png", snapshot_path(path)) for path in paths]
        self.reconciliations = []
        self.review_candidates = []

    def iter_route_candidates_by_prefix(self, run_id, mime_prefix):
        yield from self.rows

    def store_review_candidates(self, run_id, candidates):
        self.review_candidates.extend(candidates)

    def reconcile_review_candidates_batch(self, run_id, route_name, reconciliations):
        self.reconciliations.extend(reconciliations)
        return 0


def _images(root, *names):
    paths = []
    for name in names:
        path = root / name
        with Image.new("RGB", (32, 32), "navy") as image:
            image.save(path)
        paths.append(path)
    return paths


def _route(root, state, run_id=1, *, cancellation=None, memory_gate=None, **overrides):
    config = ImageRouteConfig(
        state_path=root / "state" / "image.sqlite3",
        root=root,
        workers=1,
        memory_budget_bytes=512 * MIB,
        min_free_memory_bytes=128 * MIB,
        min_free_commit_bytes=128 * MIB,
        document_ocr_mode="never",
        isolate_decoders=False,
    )
    return ImageRoute(
        replace(config, **overrides),
        state,
        run_id,
        cancellation=cancellation,
        memory_gate=memory_gate,
    )


class _ObservedMemoryGate(WeightedMemoryGate):
    """Record real admissions without replacing their waits or resource probe."""

    def __init__(self, cancellation, *, timeout=1.0):
        super().__init__(
            MemoryResourceLimits(
                memory_budget_bytes=512 * MIB,
                # Deliberately impossible fixture floor; both baseline and fixed
                # route use the same real probe and the same bounded timeout.
                min_free_memory_bytes=1 << 60,
                min_free_commit_bytes=128 * MIB,
                wait_timeout_seconds=timeout,
            ),
            cancellation,
        )
        self.entered = threading.Event()
        self.failures = []
        self.started = 0

    @contextmanager
    def admit(self, estimated_bytes):
        self.started += 1
        self.entered.set()
        try:
            with super().admit(estimated_bytes):
                yield
        except BaseException as error:
            self.failures.append((error, time.monotonic()))
            raise


@pytest.mark.parametrize("coordinated", [False, True])
def test_first_real_headroom_timeout_stops_second_admission(
    tmp_path, monkeypatch, coordinated
):
    state = _State(_images(tmp_path, "a.png", "b.png"))
    cancellation = CancellationToken()
    gate = _ObservedMemoryGate(cancellation)
    if coordinated:
        coordinator = GlobalResourceCoordinator(
            ("image",),
            GlobalResourceLimits(
                memory_budget_bytes=512 * MIB,
                min_free_memory_bytes=1 << 60,
                min_free_commit_bytes=128 * MIB,
                wait_timeout_seconds=1.0,
            ),
            cancellation=cancellation,
        )
        active_gate = CoordinatedMemoryGate(coordinator, "image")
        real_admit = active_gate.admit

        @contextmanager
        def observed_admit(estimated_bytes):
            try:
                with real_admit(estimated_bytes):
                    yield
            except BaseException as error:
                gate.failures.append((error, time.monotonic()))
                raise

        monkeypatch.setattr(active_gate, "admit", observed_admit)
    else:
        active_gate = gate
    route = _route(tmp_path, state, cancellation=cancellation, memory_gate=active_gate)
    submitted = []

    class ObservedExecutor(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            submitted.append(future)
            return future

    monkeypatch.setattr(
        "neocortex.capabilities.formats.image.route.ThreadPoolExecutor", ObservedExecutor
    )
    with pytest.raises(MemoryHeadroomTimeout) as raised:
        route.run()
    finished = time.monotonic()

    assert len(submitted) == 2
    assert all(future.done() for future in submitted)
    timeouts = [(error, stamp) for error, stamp in gate.failures
                if isinstance(error, MemoryHeadroomTimeout)]
    assert len(timeouts) == 1
    assert raised.value is timeouts[0][0]
    assert finished - timeouts[0][1] < 0.75
    assert cancellation.is_cancelled
    assert submitted[1].cancelled() or isinstance(
        submitted[1].exception(), CancellationRequested
    )
    assert gate._reserved == 0
    assert not gate._headroom_admission_lock.locked()
    if coordinated:
        assert coordinator._reserved_bytes == 0
        assert coordinator._active_requests == 0
        assert all(not queue for queue in coordinator._queues.values())
    assert route._supervisors == set()


def test_external_cancellation_releases_active_admissions(tmp_path):
    state = _State(_images(tmp_path, "a.png", "b.png", "c.png", "d.png"))
    cancellation = CancellationToken()
    gate = _ObservedMemoryGate(cancellation, timeout=5.0)
    route = _route(
        tmp_path, state, cancellation=cancellation, memory_gate=gate, workers=2
    )
    errors = []

    def run_route():
        try:
            route.run()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_route)
    thread.start()
    try:
        assert gate.entered.wait(2)
        cancellation.cancel()
        thread.join(2)
        assert not thread.is_alive()
    finally:
        cancellation.cancel()
        thread.join(6)

    assert len(errors) == 1 and isinstance(errors[0], CancellationRequested)
    assert not any(isinstance(error, MemoryHeadroomTimeout) for error, _ in gate.failures)
    assert gate._reserved == 0
    assert not gate._headroom_admission_lock.locked()


@pytest.mark.parametrize("error_type", [MemoryHeadroomTimeout, MemoryError, KeyboardInterrupt])
def test_abort_keeps_original_error_when_cleanup_also_fails(
    tmp_path, monkeypatch, error_type
):
    state = _State(_images(tmp_path, "a.png"))
    route = _route(tmp_path, state)
    primary = error_type("first failure")
    closed = []

    def fail_analysis(*args):
        raise primary

    def fail_flush(*args):
        raise OSError("fixture persistence unavailable")

    original_close_rows = route._close_candidate_rows

    def close_rows_then_fail(rows):
        original_close_rows(rows)
        closed.append("rows")
        raise OSError("fixture cursor cleanup failed")

    def fail_worker_close():
        closed.append("workers")
        raise OSError("fixture worker cleanup failed")

    monkeypatch.setattr(route, "_analyze", fail_analysis)
    monkeypatch.setattr(route, "_flush_result_batches", fail_flush)
    monkeypatch.setattr(route, "_close_candidate_rows", close_rows_then_fail)
    monkeypatch.setattr(route, "_close_image_workers", fail_worker_close)

    with pytest.raises(error_type) as raised:
        route.run()

    assert raised.value is primary
    assert route.cancellation.is_cancelled
    assert closed == ["rows", "workers"]
    assert len(primary.__notes__) == 3
    assert "result flush" in primary.__notes__[0]
    assert "candidate cursor close" in primary.__notes__[1]
    assert "worker close" in primary.__notes__[2]


def test_completed_result_survives_fatal_sibling_and_is_reused(tmp_path, monkeypatch):
    first, second = _images(tmp_path, "a-completed.png", "b-failing.png")
    state = _State([first, second])
    route = _route(tmp_path, state)
    primary = MemoryHeadroomTimeout("fixture pressure after one completed result")
    completed = threading.Event()
    analyze = route._analyze

    def fail_second(snapshot, cached_features=None):
        if snapshot.path == str(second):
            assert completed.wait(5), "the first result was not consumed"
            raise primary
        return analyze(snapshot, cached_features)

    def record_progress(event):
        if event.operation == "image" and event.phase == "classify" and event.completed == 1:
            completed.set()

    route.progress = record_progress
    monkeypatch.setattr(route, "_analyze", fail_second)
    with pytest.raises(MemoryHeadroomTimeout) as raised:
        route.run()
    assert raised.value is primary
    assert route.memory_gate._reserved == 0
    rows = list(iter_candidates(route.config.state_path, 1, None, None))
    assert {Path(row["path"]).name: row["status"] for row in rows} == {
        first.name: "done", second.name: "pending"
    }

    resumed = _route(tmp_path, state, run_id=2).run()
    assert resumed.cache_hits == 1
    assert resumed.classified == 1
    assert resumed.errors == 0


def test_item_budget_failure_remains_nonfatal(tmp_path):
    state = _State(_images(tmp_path, "a.png", "b.png"))
    route = _route(tmp_path, state, memory_budget_bytes=1)

    summary = route.run()

    assert summary.processed == 2
    assert summary.errors == 2
    assert not route.cancellation.is_cancelled
    assert route.memory_gate._reserved == 0


def _blocked_decoder(task_channel, result_channel):
    """Spawnable real child: publish readiness and wait for owner termination."""
    del result_channel
    task = task_channel.get()
    if task is None:
        return
    Path(task[1]).with_suffix(".started").write_text(str(os.getpid()), encoding="ascii")
    time.sleep(30)


def test_fatal_sibling_terminates_active_isolated_decoder(tmp_path, monkeypatch):
    blocking, failing = _images(tmp_path, "a-blocked.png", "b-fatal.png")
    state = _State([blocking, failing])
    route = _route(tmp_path, state, workers=2, isolate_decoders=True)
    primary = MemoryHeadroomTimeout("fixture fatal sibling admission")
    ready = blocking.with_suffix(".started")
    supervisors = []
    failed_at = []

    def make_supervisor():
        supervisor = ImageWorkerSupervisor(_blocked_decoder)
        supervisors.append(supervisor)
        return supervisor

    monkeypatch.setattr(
        "neocortex.capabilities.formats.image.route.ImageWorkerSupervisor", make_supervisor
    )
    analyze = route._analyze

    def fail_when_child_is_running(snapshot, cached_features=None):
        if snapshot.path == str(failing):
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "isolated decoder did not start"
            failed_at.append(time.monotonic())
            raise primary
        return analyze(snapshot, cached_features)

    monkeypatch.setattr(route, "_analyze", fail_when_child_is_running)
    with pytest.raises(MemoryHeadroomTimeout) as raised:
        route.run()

    assert raised.value is primary
    assert time.monotonic() - failed_at[0] < 3
    assert supervisors and all(supervisor._process is None for supervisor in supervisors)
    assert all(supervisor._task_channel is None for supervisor in supervisors)
    assert all(supervisor._result_channel is None for supervisor in supervisors)
    assert route._supervisors == set()
    assert route.memory_gate._reserved == 0
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(int(ready.read_text(encoding="ascii")), 0)


def test_all_supervisors_close_even_when_one_close_fails(tmp_path):
    route = _route(tmp_path, _State([]))
    primary = RuntimeError("fixture close failure")
    closed = []

    class Supervisor:
        def __init__(self, fails):
            self.fails = fails

        def close(self):
            closed.append(self)
            if self.fails:
                raise primary

    failing, healthy = Supervisor(True), Supervisor(False)
    route._supervisors.update((failing, healthy))

    with pytest.raises(RuntimeError) as raised:
        route._close_image_workers()

    assert raised.value is primary
    assert set(closed) == {failing, healthy}
    assert route._supervisors == set()
