"""Code parsing uses elastic processes while its SQLite writer stays with the owner."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.code.code_contracts import CodeAnalysis, CodeFileInput, CodeRouteConfig
from neocortex.code.code_route import CodeRoute
from neocortex.code.code_state import CodeState
from neocortex.code.ingestion.code_analyzers import (
    BUILTIN_ANALYZERS,
    AnalyzerRegistry,
    AnalyzerSpec,
)
from neocortex.code.ingestion.code_python import PythonAnalyzer
from neocortex.deduplication import FileSnapshot
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
)


class RecordedPythonAnalyzer(PythonAnalyzer):
    """Trusted test parser records actual child overlap, never runs source code."""

    analyzer_id = "recorded-python-worker"
    analyzer_version = "1"

    def analyze(self, source: CodeFileInput, config: CodeRouteConfig) -> CodeAnalysis:
        started = time.monotonic_ns()
        # Make concurrent execution observable independently of machine speed.
        time.sleep(0.15)
        result = super().analyze(source, config)
        return replace(
            result,
            provenance={
                **result.provenance,
                "worker_pid": os.getpid(),
                "worker_started": started,
                "worker_finished": time.monotonic_ns(),
            },
        )


def _registry() -> AnalyzerRegistry:
    return AnalyzerRegistry(
        (
            AnalyzerSpec(
                RecordedPythonAnalyzer.analyzer_id,
                frozenset({"python"}),
                __name__,
                "RecordedPythonAnalyzer",
                RecordedPythonAnalyzer.analyzer_version,
                priority=0,
            ),
            *BUILTIN_ANALYZERS,
        )
    )


class _Inventory:
    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths

    def snapshots(self, _scan_id: int):
        for path in self.paths:
            observed = path.stat()
            yield FileSnapshot(
                str(path),
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
                getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
            )


class _Framework:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def begin_route_phase(self, *_args, **_kwargs):
        pass

    def complete_route_phase(self, *_args, **_kwargs):
        pass

    def fail_route_phase(self, _run_id, _route_name, phase_name, _exc):
        self.failed.append(phase_name)


class _ElasticGate:
    """Controlled availability; admissions observe the real result lifetime."""

    def __init__(self, *, adaptive: bool = False) -> None:
        self.adaptive = adaptive
        self.stored = 0
        self.active = 0
        self.maximum = 0
        self.targets: list[int] = []
        self.resource_fields: list[dict[str, object]] = []
        self.lock = threading.Lock()

    def worker_capacity(self, **_kwargs) -> int:
        target = 1 if self.adaptive and 3 <= self.stored < 9 else 3
        self.targets.append(target)
        return target

    @contextmanager
    def admit(self, _estimated_bytes: int, **resources):
        execution = resources.get("cpu_slots", 1) != 0
        with self.lock:
            self.active += int(execution)
            self.maximum = max(self.maximum, self.active)
            self.resource_fields.append(resources)
        try:
            yield None
        finally:
            with self.lock:
                self.active -= int(execution)


def _fixture(tmp_path: Path, count: int = 3):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    paths = [corpus / f"worker_{index:02}.py" for index in range(count)]
    for index, path in enumerate(paths):
        path.write_text(
            f"def value_{index}():\n    return {index}\n"
            "raise RuntimeError('observed source must never execute')\n",
            encoding="utf-8",
        )
    config = CodeRouteConfig(tmp_path / "state" / "code.sqlite3", tmp_path / "dedup.sqlite3")
    return config, _Inventory(paths), {path: path.read_bytes() for path in paths}


def _max_overlap(provenances: list[dict[str, int]]) -> int:
    events = [
        event
        for item in provenances
        for event in [(item["worker_started"], 1), (item["worker_finished"], -1)]
    ]
    active = 0
    maximum = 0
    for _timestamp, delta in sorted(events):
        active += delta
        maximum = max(maximum, active)
    return maximum


def test_code_processes_contract_and_recover_capacity_during_the_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, inventory, originals = _fixture(tmp_path, count=16)
    gate = _ElasticGate(adaptive=True)
    owner_thread = threading.get_ident()
    original_store = CodeState.store_analysis
    children_before = {child.pid for child in multiprocessing.active_children()}

    def store_in_owner(state, analysis, run_id):
        assert threading.get_ident() == owner_thread
        assert gate.active > 0, "result must keep its admission through SQLite persistence"
        result = original_store(state, analysis, run_id)
        gate.stored += 1
        return result

    monkeypatch.setattr(CodeState, "store_analysis", store_in_owner)
    summary = CodeRoute(
        config, inventory, _Framework(), 1, 1, analyzers=_registry(), memory_gate=gate
    ).run()

    with sqlite3.connect(config.state_path) as connection:
        rows = connection.execute(
            "SELECT path_observed,provenance_json FROM file_versions ORDER BY path_observed"
        ).fetchall()
    evidence = [json.loads(provenance) for _path, provenance in rows]
    assert summary.processed == summary.candidates == 16
    assert summary.errors == 0
    assert gate.active == 0
    assert 1 in gate.targets
    assert gate.targets[0] == gate.targets[-1] == 3
    assert gate.maximum == 3
    assert all(item["worker_pid"] != os.getpid() for item in evidence)
    assert _max_overlap(evidence[:3]) >= 2
    assert _max_overlap(evidence[6:9]) == 1
    assert _max_overlap(evidence[11:]) >= 2
    assert {child.pid for child in multiprocessing.active_children()} == children_before
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    analysis_resources = [item for item in gate.resource_fields if item.get("phase") == "analysis"]
    assert len(analysis_resources) == 16
    assert all(item["native_threads"] == item["io_slots"] == 1 for item in analysis_resources)
    assert all(item["io_device"] for item in analysis_resources)

    # A metadata replay avoids parser processes but admits FTS/cache work.
    warm_gate = _ElasticGate()
    with sqlite3.connect(config.state_path) as connection:
        connection.execute("DELETE FROM code_fts")
    repair = CodeState._repair_cached_fts

    def admitted_repair(state, *args):
        assert threading.get_ident() == owner_thread
        assert warm_gate.active > 0
        return repair(state, *args)

    monkeypatch.setattr(CodeState, "_repair_cached_fts", admitted_repair)
    replay = CodeRoute(
        config, inventory, _Framework(), 2, 2, analyzers=_registry(), memory_gate=warm_gate
    ).run()
    assert replay.cache_hits == 16
    assert replay.processed == 0
    assert replay.fts_rows_repaired > 0
    assert replay.graph_generation_reused == 1
    assert len([item for item in warm_gate.resource_fields if item.get("phase") == "analysis"]) == 16
    assert {child.pid for child in multiprocessing.active_children()} == children_before


def test_full_code_cache_prepares_in_owner_and_skips_process_compute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, inventory, originals = _fixture(tmp_path)
    config = replace(config, cache_validation="full")
    CodeRoute(config, inventory, _Framework(), 1, 1, memory_gate=_ElasticGate()).run()
    gate = _ElasticGate()
    original_reuse = CodeState.reuse_cached
    owner = threading.get_ident()

    def reuse_in_owner(state, *args, **kwargs):
        assert threading.get_ident() == owner
        assert gate.active > 0
        return original_reuse(state, *args, **kwargs)

    def unnecessary_analysis(_task):
        raise AssertionError("full cache hit must bypass process analysis")

    monkeypatch.setattr(CodeState, "reuse_cached", reuse_in_owner)
    monkeypatch.setattr("neocortex.code.code_route.process_code_candidate", unnecessary_analysis)
    summary = CodeRoute(config, inventory, _Framework(), 2, 2, memory_gate=gate).run()
    assert summary.cache_hits == 3
    assert summary.processed == 0
    assert summary.bytes_read == sum(len(raw) for raw in originals.values())
    assert gate.active == 0


def test_oversized_code_metadata_is_published_with_a_real_owner_lease(tmp_path: Path) -> None:
    config, inventory, originals = _fixture(tmp_path)
    originals = {path: raw * 64 for path, raw in originals.items()}
    for path, raw in originals.items():
        path.write_bytes(raw)
    config = replace(config, max_file_bytes=4096)
    mib = 1024**2
    coordinator = GlobalResourceCoordinator(
        ("code",),
        GlobalResourceLimits(
            memory_budget_bytes=512 * mib, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=2, native_thread_slots=2,
            wait_timeout_seconds=3, poll_interval_seconds=0.005,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=1024 * mib, available_commit=1024 * mib,
            total_physical=2048 * mib, total_commit=2048 * mib,
            cpu_load_percent=0, external_cpu_cores=0, effective_cpu_capacity=2,
        ),
        effective_cpu_probe=lambda: 2,
    )
    result = CodeRoute(
        config, inventory, _Framework(), 1, 1,
        memory_gate=CoordinatedMemoryGate(coordinator, "code"),
    ).run()
    assert result.skipped_limit == 3
    assert result.errors == 0
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    measured = coordinator.summary()
    assert measured.peak_cpu_slots <= 2
    assert measured.resident_bytes == measured.transient_bytes == measured.cpu_slots_in_use == 0


def test_code_cancellation_drains_owned_processes_and_releases_pending_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, inventory, originals = _fixture(tmp_path, count=10)
    gate = _ElasticGate()
    cancellation = CancellationToken()
    framework = _Framework()
    original_store = CodeState.store_analysis
    stored: list[str] = []
    children_before = {child.pid for child in multiprocessing.active_children()}

    def cancel_after_first(state, analysis, run_id):
        result = original_store(state, analysis, run_id)
        stored.append(analysis.input.snapshot.path)
        cancellation.cancel()
        return result

    monkeypatch.setattr(CodeState, "store_analysis", cancel_after_first)
    with pytest.raises(CancellationRequested):
        CodeRoute(
            config,
            inventory,
            framework,
            1,
            1,
            analyzers=_registry(),
            memory_gate=gate,
            cancellation=cancellation,
        ).run()
    assert len(stored) == 1
    assert framework.failed == ["analysis"]
    assert gate.active == 0
    assert {child.pid for child in multiprocessing.active_children()} == children_before
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    with sqlite3.connect(config.state_path) as connection:
        assert connection.execute("SELECT status FROM analysis_runs").fetchone() == ("cancelled",)


def test_code_revalidates_a_completed_worker_before_publishing(tmp_path: Path) -> None:
    config, inventory, _originals = _fixture(tmp_path, count=1)

    class ConcurrentChangeRoute(CodeRoute):
        def _store_candidate_result(self, state, outcome, counters, elapsed_nanoseconds):
            inventory.paths[0].write_text("# synthetic concurrent replacement\n", encoding="utf-8")
            return super()._store_candidate_result(state, outcome, counters, elapsed_nanoseconds)

    summary = ConcurrentChangeRoute(
        config, inventory, _Framework(), 1, 1, memory_gate=_ElasticGate()
    ).run()
    assert summary.stale_inventory == summary.errors == 1
    with sqlite3.connect(config.state_path) as connection:
        assert connection.execute("SELECT analysis_status FROM file_versions").fetchone() == (
            "error",
        )
