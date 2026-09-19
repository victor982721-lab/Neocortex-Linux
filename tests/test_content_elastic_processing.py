"""Content routes use actual processes and retain their results through writing."""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import neocortex.capabilities.formats.docx.route as docx
import neocortex.capabilities.formats.office.route as office
import neocortex.capabilities.formats.text.text_processing as text_processing
import neocortex.capabilities.formats.text.text_route as text
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits, ResourceSample,
)
from tests.test_docx_route import _State, _make_docx
from tests.test_office_route import FakeFrameworkRouteState, _write_typed_xlsx
from tests.test_text_route import FakeTextFrameworkState

_REAL_DOCX = docx._extract_docx_work
_REAL_OFFICE = office._extract_office_work
_REAL_TEXT = text_processing.parse_text_work


def _record(work, operation):
    started = time.monotonic_ns()
    time.sleep(0.12)
    result = operation(work)
    Path(work.snapshot.path + ".probe.json").write_text(json.dumps({
        "pid": os.getpid(), "start": started, "end": time.monotonic_ns(),
    }))
    return result


def _record_docx(work):
    return _record(work, _REAL_DOCX)


def _record_office(work):
    return _record(work, _REAL_OFFICE)


def _record_text(work):
    return _record(work, _REAL_TEXT)


def _slow_text(work):
    def slow(*args, **kwargs):
        time.sleep(0.15)
        return work.extractor(*args, **kwargs)

    return _REAL_TEXT(replace(work, extractor=slow))


class _Gate:
    def __init__(self, adaptive=False):
        self.adaptive = adaptive
        self.stored = 0
        self.active = 0
        self.resident = 0
        self.maximum = 0
        self.targets = []
        self.peak_reserved_bytes = 0
        self.wait_count = 0
        self.lock = threading.Lock()

    def worker_capacity(self, **_kwargs):
        target = 1 if self.adaptive and 3 <= self.stored < 9 else 3
        self.targets.append(target)
        return target

    @contextmanager
    def admit(self, estimated_bytes, **resources):
        resident = resources.get("cpu_slots", 1) == 0
        with self.lock:
            if resident:
                self.resident += 1
            else:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            self.peak_reserved_bytes = max(self.peak_reserved_bytes, estimated_bytes)
        try:
            yield None
        finally:
            with self.lock:
                if resident:
                    self.resident -= 1
                else:
                    self.active -= 1


def _overlap(evidence):
    events = [event for item in evidence for event in ((item["start"], 1), (item["end"], -1))]
    active = maximum = 0
    for _when, delta in sorted(events):
        active += delta
        maximum = max(maximum, active)
    return maximum


def _route(tmp_path, kind, gate, count=16, **config):
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    suffix = {"docx": ".docx", "office": ".xlsx", "text": ".txt"}[kind]
    paths = [corpus / f"document-{index:02}{suffix}" for index in range(count)]
    for path in paths:
        if kind == "docx":
            _make_docx(path)
        elif kind == "office":
            _write_typed_xlsx(path)
        else:
            path.write_text("Contenido independiente con identidad verificable.")
    snapshots = tuple(snapshot_path(path) for path in paths)
    database = tmp_path / f"{kind}.sqlite3"
    if kind == "docx":
        route = docx.DocxRoute(
            docx.DocxRouteConfig(database, **config),
            _State({docx.DOCX_MIME: snapshots, docx.PDF_MIME: ()}), 1, memory_gate=gate,
        )
    elif kind == "office":
        route = office.OfficeRoute(
            office.OfficeRouteConfig(database, **config),
            FakeFrameworkRouteState({office.XLSX_MIME: snapshots}), 1, memory_gate=gate,
        )
    else:
        route = text.TextRoute(
            text.TextRouteConfig(database, **config),
            FakeTextFrameworkState({"text/plain": snapshots}), 1, memory_gate=gate,
        )
    return route, paths, {path: path.read_bytes() for path in paths}


@pytest.mark.parametrize("kind", ["docx", "office", "text"])
def test_content_processes_shrink_and_recover_with_owner_and_result_reservation(
    tmp_path, monkeypatch, kind,
):
    gate = _Gate(adaptive=True)
    route, paths, originals = _route(tmp_path, kind, gate)
    module, name, worker = {
        "docx": (docx, "_extract_docx_work", _record_docx),
        "office": (office, "_extract_office_work", _record_office),
        "text": (text_processing, "parse_text_work", _record_text),
    }[kind]
    monkeypatch.setattr(module, name, worker)
    owner = threading.get_ident()
    store_owner = office if kind == "office" else route
    original_store = store_owner._store_success

    def store(*args, **kwargs):
        assert threading.get_ident() == owner
        assert gate.active > 0, "result bytes must remain admitted until persistence finishes"
        value = original_store(*args, **kwargs)
        gate.stored += 1
        return value

    monkeypatch.setattr(store_owner, "_store_success", store)
    children = {child.pid for child in multiprocessing.active_children()}
    summary = route.run()
    evidence = [json.loads(Path(str(path) + ".probe.json").read_text()) for path in paths]
    assert summary.extracted == 16
    assert summary.errors == 0
    assert gate.active == gate.resident == 0
    assert gate.maximum == 3
    assert 1 in gate.targets
    assert gate.targets[0] == gate.targets[-1] == 3
    assert all(item["pid"] != os.getpid() for item in evidence)
    assert _overlap(evidence[:3]) >= 2
    assert _overlap(evidence[6:9]) == 1
    assert _overlap(evidence[11:]) >= 2
    assert {child.pid for child in multiprocessing.active_children()} == children
    assert all(path.read_bytes() == content for path, content in originals.items())

    def unexpected_work(_work):
        raise AssertionError("compatible cache must bypass pure processing")

    monkeypatch.setattr(module, name, unexpected_work)
    route.run_id = 2
    replay = route.run()
    assert replay.cache_hits == 16
    assert replay.extracted == 0
    assert gate.active == gate.resident == 0


@pytest.mark.parametrize("limits", [
    {"worker_timeout_seconds": 0.02}, {"worker_memory_bytes": 1},
])
def test_text_process_enforces_configured_limits_and_publishes_failure(
    tmp_path, monkeypatch, limits,
):
    route, _paths, originals = _route(tmp_path, "text", _Gate(), count=1, **limits)
    monkeypatch.setattr(text_processing, "parse_text_work", _slow_text)
    summary = route.run()
    assert summary.extracted == 0
    assert summary.errors == summary.retryable_errors == 1
    assert all(path.read_bytes() == content for path, content in originals.items())
    with text.text_database(route.config.state_path, create=False) as connection:
        statuses = connection.execute("SELECT status FROM text_derivation_attempts").fetchall()
        assert [row[0] for row in statuses] == ["failed"]


@pytest.mark.parametrize("kind", ["docx", "office"])
def test_legacy_content_reservation_includes_writer(tmp_path, monkeypatch, kind):
    class LegacyGate:
        def __init__(self):
            self.active = 0
            self.peak_reserved_bytes = 0
            self.wait_count = 0

        @contextmanager
        def admit(self, _estimated_bytes):
            self.active += 1
            try:
                yield
            finally:
                self.active -= 1

    gate = LegacyGate()
    route, _paths, _originals = _route(tmp_path, kind, gate, count=1)
    store_owner = office if kind == "office" else route
    original = store_owner._store_success

    def store(*args, **kwargs):
        assert gate.active == 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store_owner, "_store_success", store)
    assert route.run().extracted == 1
    assert gate.active == 0


def test_docx_elastic_cache_conflict_does_not_abort_other_identities(tmp_path, monkeypatch):
    gate = _Gate()
    route, paths, originals = _route(tmp_path, "docx", gate, count=3)
    classify = route._cache_status

    def conflict(connection, snapshot, **kwargs):
        if snapshot.path == str(paths[0]):
            raise docx._LiveDocxCachePathConflict("another live identity owns this path")
        return classify(connection, snapshot, **kwargs)

    monkeypatch.setattr(route, "_cache_status", conflict)
    summary = route.run()
    assert (summary.extracted, summary.errors, summary.new_documents) == (2, 1, 2)
    assert gate.active == gate.resident == 0
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_text_elastic_unavailable_capability_keeps_typed_failure(tmp_path, monkeypatch):
    route, _paths, _originals = _route(tmp_path, "text", _Gate(), count=1)
    select = text.CapabilityBroker.select

    def unavailable(*args, **kwargs):
        selection = select(*args, **kwargs)
        return replace(selection, selected=None, candidates=(), explanation=("unavailable",))

    def unexpected_work(_work):
        raise AssertionError("an unavailable capability must not launch its parser")

    monkeypatch.setattr(text.CapabilityBroker, "select", unavailable)
    monkeypatch.setattr(text_processing, "parse_text_work", unexpected_work)
    summary = route.run()
    assert (summary.extracted, summary.errors) == (0, 1)
    with text.text_database(route.config.state_path, create=False) as connection:
        error = connection.execute("SELECT error_type FROM documents").fetchone()[0]
        assert error == "TextCapabilityUnavailableError"


@pytest.mark.parametrize("kind", ["docx", "office", "text"])
def test_content_cancellation_discards_queued_results_and_releases_children(
    tmp_path, monkeypatch, kind,
):
    gate = _Gate()
    route, _paths, originals = _route(tmp_path, kind, gate, count=8)
    store_owner = office if kind == "office" else route
    original = store_owner._store_success
    writes = []

    def cancel_after_first_store(*args, **kwargs):
        writes.append(threading.get_ident())
        result = original(*args, **kwargs)
        route.cancellation.cancel()
        return result

    monkeypatch.setattr(store_owner, "_store_success", cancel_after_first_store)
    children = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(CancellationRequested):
        route.run()
    assert writes == [threading.get_ident()]
    assert gate.active == gate.resident == 0
    assert {child.pid for child in multiprocessing.active_children()} == children
    assert all(path.read_bytes() == content for path, content in originals.items())
    if kind == "text":
        with text.text_database(route.config.state_path, create=False) as connection:
            unfinished = connection.execute(
                "SELECT COUNT(*) FROM text_derivation_attempts WHERE status='running'",
            ).fetchone()[0]
            assert unfinished == 0


def _coordinator(route_name, budget_mib, cancellation):
    mib = 1024 * 1024
    return GlobalResourceCoordinator(
        (route_name,),
        GlobalResourceLimits(
            memory_budget_bytes=budget_mib * mib, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=8, native_thread_slots=8,
            wait_timeout_seconds=2, poll_interval_seconds=0.01,
        ),
        cancellation=cancellation, effective_cpu_probe=lambda: 8,
        resource_probe=lambda: ResourceSample(
            available_physical=2048 * mib, available_commit=2048 * mib,
            total_physical=4096 * mib, total_commit=4096 * mib,
            external_cpu_cores=0.0, own_cpu_cores=0.0, cpu_load_percent=0.0,
        ),
    )


def test_docx_rejects_one_oversized_document_and_processes_the_next(tmp_path, monkeypatch):
    cancellation = CancellationToken()
    coordinator = _coordinator("docx", 256, cancellation)
    route, paths, originals = _route(
        tmp_path, "docx", CoordinatedMemoryGate(coordinator, "docx"), count=2,
    )
    route.cancellation = cancellation
    estimate = docx._estimate_docx_work

    def oversized_first(work):
        return 300 * 1024 * 1024 if work.snapshot.path == str(paths[0]) else estimate(work)

    monkeypatch.setattr(docx, "_estimate_docx_work", oversized_first)
    monkeypatch.setattr(docx, "_extract_docx_work", _record_docx)
    deadline = threading.Timer(8.0, cancellation.cancel)
    deadline.start()
    try:
        summary = route.run()
        assert (summary.extracted, summary.errors, summary.retryable_errors) == (1, 1, 1)
        assert summary.new_documents == 2
        assert not Path(str(paths[0]) + ".probe.json").exists()
        assert Path(str(paths[1]) + ".probe.json").exists()
        assert not cancellation.is_cancelled
    finally:
        deadline.cancel()
        coordinator.close()
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_docx_cache_replay_reserves_representation_instead_of_original_parse(
    tmp_path, monkeypatch,
):
    route, _paths, originals = _route(tmp_path, "docx", _Gate(), count=2)
    assert route.run().extracted == 2
    cancellation = CancellationToken()
    coordinator = _coordinator("docx", 256, cancellation)
    route.run_id = 2
    route.cancellation = cancellation
    route.memory_gate = CoordinatedMemoryGate(coordinator, "docx")

    def unexpected_work(_work):
        raise AssertionError("a validated cache must not invoke the parser")

    monkeypatch.setattr(docx, "_estimate_docx_work", lambda _work: 300 * 1024 * 1024)
    monkeypatch.setattr(docx, "_extract_docx_work", unexpected_work)
    deadline = threading.Timer(8.0, cancellation.cancel)
    deadline.start()
    try:
        summary = route.run()
        assert (summary.cache_hits, summary.extracted, summary.errors) == (2, 0, 0)
        assert not cancellation.is_cancelled
    finally:
        deadline.cancel()
        coordinator.close()
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_text_reuses_one_process_with_a_small_real_global_memory_budget(tmp_path):
    mib = 1024 * 1024
    cancellation = CancellationToken()
    coordinator = _coordinator("text", 128, cancellation)
    route, _paths, originals = _route(
        tmp_path, "text", CoordinatedMemoryGate(coordinator, "text"), count=3,
    )
    route.cancellation = cancellation
    deadline = threading.Timer(8.0, cancellation.cancel)
    deadline.start()
    try:
        assert route.run().extracted == 3
        assert not cancellation.is_cancelled
        assert coordinator.summary().peak_reserved_bytes <= 128 * mib
    finally:
        deadline.cancel()
        coordinator.close()
    assert all(path.read_bytes() == content for path, content in originals.items())
