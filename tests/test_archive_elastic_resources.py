"""Real member concurrency plus separately admitted bounded PDF/OCR children."""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive import route as archive_route
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import resource_grant_scope
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
)


_ORIGINAL_MEMBER_EXTRACTOR = archive_route._extract_archive_member


def _recorded_member(work):
    started = time.monotonic_ns()
    time.sleep(0.12)
    result = _ORIGINAL_MEMBER_EXTRACTOR(work)
    event = [work.name, os.getpid(), started, time.monotonic_ns()]
    path = work.config.state_path.parent / "worker-events.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(event) + "\n").encode())
    finally:
        os.close(descriptor)
    return result


class _Framework:
    def __init__(self, source: Path):
        self.source = source

    def selected_route_candidate_counts(self, *_args):
        return 1, 1

    def iter_selected_route_candidates(self, *_args):
        yield snapshot_path(self.source)


class _Gate:
    peak_reserved_bytes = 0
    wait_count = 0

    def __init__(self, *, adaptive=False):
        self.adaptive = adaptive
        self.stored = 0
        self.active = 0
        self.targets = []
        self.requests = []
        self.lock = threading.Lock()

    def worker_capacity(self, **_kwargs):
        target = 1 if self.adaptive and 3 <= self.stored < 9 else 3
        self.targets.append(target)
        return target

    @contextmanager
    def admit(self, estimated_bytes, **resources):
        with self.lock:
            self.requests.append((estimated_bytes, resources))
            self.active += 1
        try:
            yield None
        finally:
            with self.lock:
                self.active -= 1


def _make_zip(path: Path, entries) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as output:
        for name, value in entries:
            output.writestr(name, value)


def _run(source: Path, state: Path, *, gate=None, **kwargs):
    return archive_route.ArchiveRoute(
        archive_route.ArchiveRouteConfig(state, ocr_mode="never", **kwargs),
        _Framework(source),
        1,
        memory_gate=gate,
    ).run()


def _overlap(events):
    active = maximum = 0
    boundaries = [(start, 1) for _name, _pid, start, _end in events]
    boundaries += [(end, -1) for _name, _pid, _start, end in events]
    for _timestamp, delta in sorted(boundaries):
        active += delta
        maximum = max(maximum, active)
    return maximum


def test_one_zip_uses_processes_and_reduces_then_recovers_member_parallelism(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.zip"
    _make_zip(source, [(f"member_{i:02}.txt", f"Content {i} " * 4000) for i in range(16)])
    original = source.read_bytes()
    gate = _Gate(adaptive=True)
    owner = threading.get_ident()
    original_store = archive_route._store_member
    original_observe = archive_route._ArchiveObservationSpool.append
    children = {child.pid for child in multiprocessing.active_children()}

    def store(connection, *args, **kwargs):
        assert threading.get_ident() == owner
        assert gate.active > 0
        result = original_store(connection, *args, **kwargs)
        return result

    def observed(spool, observation):
        original_observe(spool, observation)
        if isinstance(observation, archive_route._MemberObservation):
            gate.stored += 1

    monkeypatch.setattr(archive_route, "_store_member", store)
    monkeypatch.setattr(archive_route._ArchiveObservationSpool, "append", observed)
    monkeypatch.setattr(archive_route, "_extract_archive_member", _recorded_member)
    state = tmp_path / "state" / "archive.sqlite3"
    summary = _run(source, state, gate=gate)
    events = sorted(json.loads(line) for line in (state.parent / "worker-events.jsonl").read_text().splitlines())
    assert summary.members_indexed == 16
    assert summary.errors == 0
    assert all(pid != os.getpid() for _name, pid, _start, _end in events)
    assert _overlap(events[:3]) >= 2
    assert _overlap(events[6:9]) == 1
    assert _overlap(events[11:]) >= 2
    assert gate.targets[0] == gate.targets[-1] == 3
    assert 1 in gate.targets
    assert gate.active == 0
    retained = [fields for _size, fields in gate.requests if fields.get("phase") == "archive-container"]
    assert len(retained) == 1
    assert retained[0]["native_threads"] == 0
    assert source.read_bytes() == original
    assert {child.pid for child in multiprocessing.active_children()} == children
    replay_gate = _Gate()
    with sqlite3.connect(state) as connection:
        connection.execute("DELETE FROM document_fts")
    refresh = archive_route._refresh_cached_container

    def admitted_refresh(*args, **kwargs):
        assert threading.get_ident() == owner
        assert replay_gate.active > 0
        return refresh(*args, **kwargs)

    monkeypatch.setattr(archive_route, "_refresh_cached_container", admitted_refresh)
    replay_summary = _run(source, state, gate=replay_gate)
    assert replay_summary.cache_hits == 1
    assert replay_summary.fts_rows_repaired == 16
    replay = [fields for _size, fields in replay_gate.requests if fields.get("phase") == "archive-container"]
    assert len(replay) == 1
    assert replay[0]["io_slots"] == 1
    assert not any(fields.get("phase") == "elastic-process-resident" for _size, fields in replay_gate.requests)


def test_archive_parallel_nested_barriers_preserve_global_budgets_and_rows(tmp_path: Path) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as output:
        output.writestr("inside.txt", "nested " * 40)
    source = tmp_path / "nested.zip"
    _make_zip(source, [("before.txt", "before " * 30), ("child.zip", inner.getvalue()), ("after.txt", "after " * 30)])
    serial = tmp_path / "serial.sqlite3"
    parallel = tmp_path / "parallel.sqlite3"
    first = _run(source, serial, max_total_text_chars=350)
    second = _run(source, parallel, gate=_Gate(), max_total_text_chars=350)
    assert first == second
    query = """SELECT member_chain,content_kind,status,text_chars,detail,error_type,
        document_role,logical_document_chain FROM documents ORDER BY member_chain"""
    with sqlite3.connect(serial) as connection:
        expected = connection.execute(query).fetchall()
    with sqlite3.connect(parallel) as connection:
        assert connection.execute(query).fetchall() == expected


class _Grant:
    _owner_grant = None

    def __init__(self):
        self.events = []

    def release_cpu(self):
        self.events.append("release_cpu")

    def checkpoint(self):
        self.events.append("checkpoint")

    def subprocess_env(self, base):
        return {**base, "OMP_NUM_THREADS": "1", "OMP_THREAD_LIMIT": "1"}

    def register_process(self, pid, started):
        self.events.append((pid, started))


@pytest.mark.parametrize("memory", [128 * 1024**2, 256 * 1024**2])
def test_archive_child_declares_memory_temporary_native_and_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory: int
) -> None:
    parent, child = _Grant(), _Grant()
    requests = []

    class Gate(_Gate):
        @contextmanager
        def admit(self, estimate, **resources):
            requests.append((estimate, resources))
            yield child

    payload = b"%PDF-synthetic fixture"

    def capture(_command, **kwargs):
        assert requests[0][1]["resident_bytes"] == memory
        assert requests[0][1]["temp_bytes"] == len(payload)
        assert requests[0][1]["native_threads"] == requests[0][1]["cpu_slots"] == 1
        assert parent.events == ["release_cpu"]
        assert kwargs["input_bytes"] == payload
        assert kwargs["memory_limit_bytes"] == memory
        assert kwargs["environment"]["OMP_THREAD_LIMIT"] == "1"
        kwargs["on_started"](12345, 987)
        return subprocess.CompletedProcess([], 0, b'{"ok":true,"text":"fixture"}', b"")

    monkeypatch.setattr(archive_route, "run_bounded_capture", capture)
    config = archive_route.ArchiveRouteConfig(
        tmp_path / "state.sqlite3", pdf_worker_memory_bytes=memory, ocr_mode="never"
    )
    with archive_route._archive_resource_scope(Gate(), CancellationToken()), resource_grant_scope(parent):
        result = archive_route._extract_media_text(payload, kind="pdf", char_limit=100, config=config)
    assert result[0] == "fixture"
    assert child.events == [(12345, 987)]
    assert parent.events == ["release_cpu", "checkpoint"]


def test_archive_cancellation_releases_queued_member_leases(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "cancel.zip"
    _make_zip(source, [(f"member_{i}.txt", "fixture") for i in range(8)])
    original = source.read_bytes()
    gate = _Gate()
    token = CancellationToken()
    store = archive_route._store_member

    def cancelled_store(*args, **kwargs):
        store(*args, **kwargs)
        token.cancel()

    monkeypatch.setattr(archive_route, "_store_member", cancelled_store)
    route = archive_route.ArchiveRoute(
        archive_route.ArchiveRouteConfig(tmp_path / "state.sqlite3", ocr_mode="never"),
        _Framework(source), 1, memory_gate=gate, cancellation=token,
    )
    with pytest.raises(CancellationRequested):
        route.run()
    assert gate.active == 0
    assert source.read_bytes() == original


@pytest.mark.parametrize("substantial", [False, True])
def test_independent_containers_overlap_and_publish_owned_spools_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, substantial: bool
) -> None:
    sources = [tmp_path / f"container_{i:02}.zip" for i in range(12)]
    for index, source in enumerate(sources):
        size = archive_route._ARCHIVE_PROCESS_MIN_BYTES if substantial else 16
        _make_zip(source, [(f"member_{index:02}.txt", f"value {index} ".ljust(size, "x"))])
    originals = {source: source.read_bytes() for source in sources}
    gate = _Gate(adaptive=True)
    owner = threading.get_ident()
    original_store = archive_route._store_member
    spools = []
    original_init = archive_route._ArchiveObservationSpool.__init__

    class Framework(_Framework):
        def selected_route_candidate_counts(self, *_args):
            return len(sources), len(sources)

        def iter_selected_route_candidates(self, *_args):
            yield from (snapshot_path(source) for source in sources)

    def opened(spool, *args):
        original_init(spool, *args)
        spools.append(spool)

    def stored(connection, *args, **kwargs):
        assert threading.get_ident() == owner
        assert connection.in_transaction
        result = original_store(connection, *args, **kwargs)
        gate.stored += 1
        return result

    monkeypatch.setattr(archive_route._ArchiveObservationSpool, "__init__", opened)
    monkeypatch.setattr(archive_route, "_store_member", stored)
    monkeypatch.setattr(archive_route, "_extract_archive_member", _recorded_member)
    state = tmp_path / "state" / "archive.sqlite3"
    summary = archive_route.ArchiveRoute(
        archive_route.ArchiveRouteConfig(state, ocr_mode="never"),
        Framework(sources[0]), 1, memory_gate=gate,
    ).run()
    events = sorted(json.loads(line) for line in (state.parent / "worker-events.jsonl").read_text().splitlines())
    assert summary.processed == summary.containers_complete == summary.members_indexed == 12
    assert _overlap(events[:3]) >= 2
    assert _overlap(events[6:9]) == 1
    assert _overlap(events[9:]) >= 2
    assert all((pid != os.getpid()) == substantial for _name, pid, _start, _end in events)
    assert any(fields.get("phase") == "elastic-process-resident" for _size, fields in gate.requests) == substantial
    assert len(spools) == 12
    assert all(spool.stream.closed for spool in spools)
    assert gate.active == 0
    assert all(source.read_bytes() == original for source, original in originals.items())


@pytest.mark.parametrize("native_slots", [1, 3])
def test_archive_real_governor_accounts_spools_and_nested_cpu_without_leaking(
    tmp_path: Path, native_slots: int
) -> None:
    source = tmp_path / "source.zip"
    _make_zip(source, [(f"member_{i:02}.txt", "indexed content" * 3000) for i in range(6)])
    mib = 1024**2
    coordinator = GlobalResourceCoordinator(
        ("archive",),
        GlobalResourceLimits(
            memory_budget_bytes=512 * mib,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            cpu_slots=3,
            native_thread_slots=native_slots,
            temp_budget_bytes=16 * mib,
            wait_timeout_seconds=5,
            poll_interval_seconds=0.005,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=1024 * mib, available_commit=1024 * mib,
            total_physical=2048 * mib, total_commit=2048 * mib,
            cpu_load_percent=0, external_cpu_cores=0, effective_cpu_capacity=3,
        ),
        effective_cpu_probe=lambda: 3,
    )
    summary = _run(
        source, tmp_path / "archive.sqlite3",
        gate=CoordinatedMemoryGate(coordinator, "archive"),
        max_total_uncompressed_bytes=4 * mib,
        max_member_bytes=64 * 1024,
        max_total_text_chars=500_000,
    )
    assert summary.members_indexed == 6
    measured = coordinator.summary()
    assert measured.peak_cpu_slots <= 3
    assert measured.peak_resident_bytes >= 64 * mib
    assert measured.peak_temp_bytes > 0
    assert measured.resident_bytes == measured.transient_bytes == measured.temp_bytes == 0
    assert measured.native_threads == 0


def test_several_archives_leave_member_headroom_under_tight_memory(tmp_path: Path) -> None:
    sources = [tmp_path / f"bounded_{index}.zip" for index in range(5)]
    for source in sources:
        _make_zip(source, [(f"member_{i}.txt", "x" * (256 * 1024)) for i in range(3)])
    mib = 1024**2
    coordinator = GlobalResourceCoordinator(
        ("archive",),
        GlobalResourceLimits(
            memory_budget_bytes=384 * mib, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=3, native_thread_slots=3,
            temp_budget_bytes=16 * mib, wait_timeout_seconds=3,
            poll_interval_seconds=0.005,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=1024 * mib, available_commit=1024 * mib,
            total_physical=2048 * mib, total_commit=2048 * mib,
            cpu_load_percent=0, external_cpu_cores=0, effective_cpu_capacity=3,
        ),
        effective_cpu_probe=lambda: 3,
    )

    class Framework(_Framework):
        def selected_route_candidate_counts(self, *_args):
            return len(sources), len(sources)

        def iter_selected_route_candidates(self, *_args):
            yield from (snapshot_path(source) for source in sources)

    summary = archive_route.ArchiveRoute(
        archive_route.ArchiveRouteConfig(
            tmp_path / "archive.sqlite3", ocr_mode="never",
            max_total_uncompressed_bytes=96 * mib, max_member_bytes=512 * 1024,
            max_total_text_chars=1024 * 1024,
        ),
        Framework(sources[0]), 1, memory_gate=CoordinatedMemoryGate(coordinator, "archive"),
    ).run()
    assert summary.containers_complete == 5
    assert summary.members_indexed == 15
    measured = coordinator.summary()
    assert measured.peak_reserved_bytes <= 384 * mib
    assert measured.resident_bytes == measured.transient_bytes == measured.temp_bytes == 0


def test_archive_spool_limit_preserves_next_container_and_releases_temporary_bytes(tmp_path: Path) -> None:
    large, small = tmp_path / "large.zip", tmp_path / "small.zip"
    _make_zip(large, [("large.txt", "x" * 8192)])
    _make_zip(small, [("small.txt", "small")])
    originals = [source.read_bytes() for source in (large, small)]
    mib = 1024**2
    coordinator = GlobalResourceCoordinator(
        ("archive",),
        GlobalResourceLimits(
            memory_budget_bytes=128 * mib, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=2, native_thread_slots=2,
            temp_budget_bytes=2048, wait_timeout_seconds=3, poll_interval_seconds=0.005,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=1024 * mib, available_commit=1024 * mib,
            total_physical=2048 * mib, total_commit=2048 * mib,
            cpu_load_percent=0, external_cpu_cores=0, effective_cpu_capacity=2,
        ),
        effective_cpu_probe=lambda: 2,
    )

    class Framework(_Framework):
        def selected_route_candidate_counts(self, *_args):
            return 2, 2

        def iter_selected_route_candidates(self, *_args):
            yield snapshot_path(large)
            yield snapshot_path(small)

    state = tmp_path / "archive.sqlite3"
    result = archive_route.ArchiveRoute(
        archive_route.ArchiveRouteConfig(
            state, ocr_mode="never", max_total_uncompressed_bytes=4 * mib,
            max_member_bytes=64 * 1024, max_total_text_chars=10_000,
        ),
        Framework(large), 1, memory_gate=CoordinatedMemoryGate(coordinator, "archive"),
    ).run()
    assert result.errors == result.containers_complete == result.members_indexed == 1
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT error_type FROM containers WHERE status='error'").fetchone() == (
            "archive_spool_limit",
        )
    assert [source.read_bytes() for source in (large, small)] == originals
    measured = coordinator.summary()
    assert measured.peak_temp_bytes <= 2048
    assert measured.temp_bytes == measured.transient_bytes == measured.resident_bytes == 0


def test_archive_source_is_revalidated_after_spooling_before_owner_publication(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.zip"
    _make_zip(source, [("source.txt", "old content")])
    extract = archive_route._extract_archive_container

    def modified(task):
        result = extract(task)
        _make_zip(source, [("source.txt", "externally changed content")])
        return result

    monkeypatch.setattr(archive_route, "_extract_archive_container", modified)
    state = tmp_path / "archive.sqlite3"
    summary = _run(source, state, gate=_Gate())
    assert summary.errors == 1
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
        assert connection.execute("SELECT error_type,retryable FROM containers").fetchone() == (
            "archive_source_changed", 1,
        )


def test_several_archives_keep_headroom_for_large_native_media_children(tmp_path, monkeypatch) -> None:
    sources = [tmp_path / f"media_{index}.zip" for index in range(4)]
    for source in sources:
        _make_zip(source, [("disguised.txt", b"%PDF-synthetic bounded subprocess fixture")])
    mib = 1024**2
    coordinator = GlobalResourceCoordinator(
        ("archive",),
        GlobalResourceLimits(
            memory_budget_bytes=384 * mib, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=3, native_thread_slots=3,
            temp_budget_bytes=16 * mib, wait_timeout_seconds=3, poll_interval_seconds=0.005,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=1024 * mib, available_commit=1024 * mib,
            total_physical=2048 * mib, total_commit=2048 * mib,
            cpu_load_percent=0, external_cpu_cores=0, effective_cpu_capacity=3,
        ),
        effective_cpu_probe=lambda: 3,
    )

    class Framework(_Framework):
        def selected_route_candidate_counts(self, *_args):
            return len(sources), len(sources)

        def iter_selected_route_candidates(self, *_args):
            yield from (snapshot_path(source) for source in sources)

    def capture(_command, **kwargs):
        assert kwargs["memory_limit_bytes"] == 192 * mib
        assert coordinator.summary().resident_bytes >= 192 * mib
        time.sleep(0.05)
        return subprocess.CompletedProcess([], 0, b'{"ok":true,"text":"media text"}', b"")

    monkeypatch.setattr(archive_route, "run_bounded_capture", capture)
    summary = archive_route.ArchiveRoute(
        archive_route.ArchiveRouteConfig(
            tmp_path / "archive.sqlite3", ocr_mode="never", pdf_worker_memory_bytes=192 * mib,
            max_total_uncompressed_bytes=96 * mib, max_member_bytes=512 * 1024,
            max_total_text_chars=1024 * 1024,
        ),
        Framework(sources[0]), 1, memory_gate=CoordinatedMemoryGate(coordinator, "archive"),
    ).run()
    assert summary.containers_complete == summary.members_indexed == 4
    measured = coordinator.summary()
    assert measured.peak_reserved_bytes <= 384 * mib
    assert measured.resident_bytes == measured.transient_bytes == measured.temp_bytes == 0


def test_native_admission_wait_uses_member_cancellation_after_sibling_failure(tmp_path, monkeypatch) -> None:
    source = tmp_path / "siblings.zip"
    _make_zip(source, [("fatal.txt", "fatal"), ("waiting.pdf", b"%PDF-fixture")])
    entered, cancelled = threading.Event(), threading.Event()
    root_token = CancellationToken()

    class Gate(_Gate):
        @contextmanager
        def admit(self, estimated_bytes, **resources):
            with super().admit(estimated_bytes, **resources):
                if resources.get("phase") == "archive-pdf-worker":
                    entered.set()
                    token = resources["cancellation"]
                    if not token.wait(3):
                        raise AssertionError("member-local cancellation was not propagated")
                    cancelled.set()
                    token.checkpoint()
                yield None

    def extraction(work):
        if work.name == "fatal.txt":
            assert entered.wait(3)
            raise RuntimeError("synthetic sibling failure")
        return _ORIGINAL_MEMBER_EXTRACTOR(work)

    monkeypatch.setattr(archive_route, "_extract_archive_member", extraction)
    gate = Gate()
    summary = archive_route.ArchiveRoute(
        archive_route.ArchiveRouteConfig(tmp_path / "archive.sqlite3", ocr_mode="never"),
        _Framework(source), 1, memory_gate=gate, cancellation=root_token,
    ).run()
    assert summary.errors == 1
    assert cancelled.is_set()
    assert not root_token.is_cancelled
    assert gate.active == 0


def test_damaged_archive_cache_is_readmitted_before_fresh_extraction(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    _make_zip(source, [("source.txt", "source content")])
    original = source.read_bytes()
    state = tmp_path / "archive.sqlite3"
    _run(source, state)
    with sqlite3.connect(state) as connection:
        connection.execute("UPDATE documents SET text_zlib=x'00'")
        connection.execute("DELETE FROM document_fts")
    gate = _Gate()
    result = _run(source, state, gate=gate)
    assert result.cache_hits == 0
    assert result.containers_complete == result.members_indexed == 1
    estimates = [size for size, fields in gate.requests if fields.get("phase") == "archive-container"]
    assert len(estimates) == 2
    assert estimates[0] < estimates[1]
    assert gate.active == 0
    assert source.read_bytes() == original


@pytest.mark.parametrize("fail_replay", [False, True])
def test_cached_apply_keeps_sqlite_owner_and_releases_owner_cpu_contexts(
    tmp_path, monkeypatch, fail_replay: bool
) -> None:
    source = tmp_path / "source.zip"
    _make_zip(source, [("note.txt", "materialized note")])
    original = source.read_bytes()
    mib = 1024**2
    coordinator = GlobalResourceCoordinator(
        ("archive",),
        GlobalResourceLimits(
            memory_budget_bytes=128 * mib, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=1, native_thread_slots=1,
            temp_budget_bytes=8 * mib, wait_timeout_seconds=3, poll_interval_seconds=0.005,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=1024 * mib, available_commit=1024 * mib,
            total_physical=2048 * mib, total_commit=2048 * mib,
            cpu_load_percent=0, external_cpu_cores=0, effective_cpu_capacity=1,
        ),
        effective_cpu_probe=lambda: 1,
    )
    owner = threading.get_ident()
    materialize = archive_route.materialize_archive
    calls = 0

    def owned_materialize(*args, **kwargs):
        nonlocal calls
        assert threading.get_ident() == owner
        assert coordinator.summary().cpu_slots_in_use == 1
        calls += 1
        return materialize(*args, **kwargs)

    monkeypatch.setattr(archive_route, "materialize_archive", owned_materialize)
    finalize = archive_route.ArchiveRoute._materialize_container

    def finalized(self, *args, **kwargs):
        finalize(self, *args, **kwargs)
        if fail_replay and calls == 2:
            raise RuntimeError("synthetic replay finalization failure")

    monkeypatch.setattr(archive_route.ArchiveRoute, "_materialize_container", finalized)
    state, destination = tmp_path / "archive.sqlite3", tmp_path / "materialized"
    options = {
        "gate": CoordinatedMemoryGate(coordinator, "archive"),
        "materialize_on_apply": True, "materialization_directory": destination,
        "max_total_uncompressed_bytes": 4 * mib, "max_member_bytes": 64 * 1024,
        "max_total_text_chars": 10_000,
    }
    assert _run(source, state, **options).members_indexed == 1
    if fail_replay:
        with pytest.raises(RuntimeError, match="synthetic replay finalization failure"):
            _run(source, state, **options)
    else:
        assert _run(source, state, **options).cache_hits == 1
    assert calls == 2
    assert [path.read_text() for path in destination.rglob("note.txt")] == ["materialized note"]
    assert source.read_bytes() == original
    measured = coordinator.summary()
    assert measured.temp_bytes == measured.resident_bytes == measured.transient_bytes == 0
    assert measured.cpu_slots_in_use == measured.native_threads == 0


def test_archive_cancellation_stops_its_live_native_capture_and_releases_leases(tmp_path, monkeypatch) -> None:
    mib = 1024**2
    token = CancellationToken()
    coordinator = GlobalResourceCoordinator(
        ("archive",),
        GlobalResourceLimits(
            memory_budget_bytes=512 * mib, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=1, native_thread_slots=1,
            temp_budget_bytes=8 * mib, wait_timeout_seconds=3, poll_interval_seconds=0.005,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=1024 * mib, available_commit=1024 * mib,
            total_physical=2048 * mib, total_commit=2048 * mib,
            cpu_load_percent=0, external_cpu_cores=0, effective_cpu_capacity=1,
        ),
        effective_cpu_probe=lambda: 1,
    )
    gate = CoordinatedMemoryGate(coordinator, "archive")
    capture = archive_route.run_bounded_capture
    children = []

    def sleeping_capture(_command, **kwargs):
        assert kwargs["cancellation"] is token
        register = kwargs["on_started"]

        def started(pid, ticks):
            register(pid, ticks)
            children.append(pid)
            token.cancel()

        kwargs["on_started"] = started
        return capture((sys.executable, "-c", "import time; time.sleep(30)"), **kwargs)

    monkeypatch.setattr(archive_route, "run_bounded_capture", sleeping_capture)
    config = archive_route.ArchiveRouteConfig(
        tmp_path / "archive.sqlite3", ocr_mode="never", pdf_worker_memory_bytes=128 * mib,
        pdf_timeout_seconds=3,
    )
    with pytest.raises(CancellationRequested), gate.admit(4 * mib, cancellation=token) as parent:
        with archive_route._archive_resource_scope(gate, token), resource_grant_scope(parent):
            archive_route._extract_media_text(b"%PDF-native cancellation fixture", kind="pdf", char_limit=100, config=config)
    assert len(children) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0], os.WNOHANG)
    measured = coordinator.summary()
    assert measured.resident_bytes == measured.transient_bytes == measured.temp_bytes == 0
    assert measured.cpu_slots_in_use == measured.native_threads == 0
