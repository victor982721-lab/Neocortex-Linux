"""Regression coverage for code-route global resource coordination."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Mapping
from unittest.mock import patch

import pytest

import neocortex.deduplication
import neocortex.code.code_route as code_route_module
from neocortex.deduplication import FileSnapshot
from neocortex.runtime.control.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from neocortex.code.code_contracts import CodeRouteConfig
from neocortex.code.code_route import (
    CodeRoute,
    estimate_code_analysis_memory_bytes,
)
from neocortex.code.code_state import CodeState
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
)
from neocortex.runtime.control.memory_runtime import MemorySnapshot
from neocortex.safety.route_filters import CandidateSelection
from neocortex.runtime.orchestration.route_registry import RouteExecutionContext, _run_code
from neocortex.persistence.sqlite_cancellation import CancellationCheck


# region [01] Deterministic collaborators


def _snapshot(path: Path) -> FileSnapshot:
    observed = path.stat()
    return FileSnapshot(
        str(path),
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
    )


class _Inventory:
    def __init__(self, paths: Iterable[Path]):
        self.paths = tuple(paths)

    def snapshots(self, _scan_id: int):
        return iter(_snapshot(path) for path in self.paths)


class _FrameworkState:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def begin_route_phase(
        self,
        _run_id: int,
        _route_name: str,
        _phase_name: str,
        *,
        source_run_id: int | None = None,
    ) -> None:
        del source_run_id

    def complete_route_phase(
        self,
        _run_id: int,
        _route_name: str,
        _phase_name: str,
        summary: Mapping[str, object] | None = None,
    ) -> None:
        assert summary is not None

    def fail_route_phase(
        self,
        _run_id: int,
        _route_name: str,
        phase_name: str,
        _exc: BaseException,
    ) -> None:
        self.failed.append(phase_name)


class _TrackingGate:
    def __init__(self) -> None:
        self.estimates: list[int] = []
        self.events: list[str] = []
        self.active = 0
        self.max_active = 0

    @contextmanager
    def admit(self, estimated_bytes: int):
        self.estimates.append(estimated_bytes)
        self.events.append("enter")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield
        finally:
            self.active -= 1
            self.events.append("exit")


class _ObservedCodeRoute(CodeRoute):
    def _analyze_bytes(self, snapshot: FileSnapshot, raw: bytes):
        assert isinstance(self.memory_gate, _TrackingGate)
        assert self.memory_gate.active == 1
        return super()._analyze_bytes(snapshot, raw)


def _config(tmp_path: Path) -> CodeRouteConfig:
    return CodeRouteConfig(
        state_path=tmp_path / "state" / "code.sqlite3",
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        max_file_bytes=1024 * 1024,
        max_text_chars=100_000,
        chunk_chars=1024,
    )


def _coordinator() -> GlobalResourceCoordinator:
    return GlobalResourceCoordinator(
        ("code",),
        GlobalResourceLimits(
            memory_budget_bytes=256 * 1024 * 1024,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            cpu_slots=1,
            wait_timeout_seconds=1,
            poll_interval_seconds=0.005,
        ),
        cpu_load_probe=lambda: 0.0,
    )


# endregion [01]


# region [02] Per-candidate and graph admission


def test_route_admits_each_analysis_and_graph_without_route_wide_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("def first():\n    return 1\n", encoding="utf-8")
    second.write_text("def second():\n    return 2\n", encoding="utf-8")
    config = _config(tmp_path)
    inventory = _Inventory((first, second))
    gate = _TrackingGate()
    original_finalize = CodeState.finalize_graph

    def checked_finalize(
        state: CodeState,
        run_id: int,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> int:
        assert gate.active == 1
        return original_finalize(
            state,
            run_id,
            cancellation_check=cancellation_check,
        )

    monkeypatch.setattr(CodeState, "finalize_graph", checked_finalize)
    summary = _ObservedCodeRoute(
        config,
        inventory,
        _FrameworkState(),
        1,
        1,
        memory_gate=gate,
    ).run()

    assert summary.processed == 2
    assert gate.estimates[:2] == [
        estimate_code_analysis_memory_bytes(first.stat().st_size, 100_000),
        estimate_code_analysis_memory_bytes(second.stat().st_size, 100_000),
    ]
    assert len(gate.estimates) == 3  # two candidates plus one graph phase
    assert gate.max_active == 1
    assert gate.active == 0
    assert gate.events == ["enter", "exit"] * 3

    warm_gate = _TrackingGate()
    gate = warm_gate
    warm = _ObservedCodeRoute(
        config,
        inventory,
        _FrameworkState(),
        2,
        1,
        memory_gate=warm_gate,
    ).run()

    assert warm.cache_hits == 2
    assert warm.processed == 0
    assert warm_gate.estimates == []  # stable metadata hits reuse the fenced graph
    assert warm_gate.events == []


def test_cancellation_inside_analysis_propagates_and_releases_admission(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cancel.py"
    source.write_text("def cancel():\n    return True\n", encoding="utf-8")
    gate = _TrackingGate()

    class CancellingRoute(CodeRoute):
        def _analyze_bytes(self, snapshot: FileSnapshot, raw: bytes):
            del snapshot, raw
            raise CancellationRequested("cancelled inside analyzer")

    framework = _FrameworkState()
    with pytest.raises(CancellationRequested, match="inside analyzer"):
        CancellingRoute(
            _config(tmp_path),
            _Inventory((source,)),
            framework,
            1,
            1,
            memory_gate=gate,
        ).run()

    assert gate.events == ["enter", "exit"]
    assert gate.active == 0
    assert framework.failed == ["analysis"]


# endregion [02]


# region [03] Registry-to-coordinator integration


def test_run_code_passes_a_releasing_coordinator_gate(tmp_path: Path) -> None:
    captured: list[object] = []

    class FakeDedupIndex:
        def __init__(self, _path: Path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeCodeRoute:
        def __init__(self, *_args: object, memory_gate=None, **_kwargs: object):
            captured.append(memory_gate)
            self.memory_gate = memory_gate

        def run(self) -> str:
            with self.memory_gate.admit(1024):
                pass
            return "coordinated"

    config = SimpleNamespace(
        root=tmp_path,
        self_analysis=False,
        dedup_database=tmp_path / "dedup.sqlite3",
        code_database=tmp_path / "code.sqlite3",
        code_max_file_bytes=1024 * 1024,
        code_max_text_chars=100_000,
        code_max_documents=None,
        code_chunk_chars=1024,
        code_retry_errors=False,
        code_cache_validation="metadata",
        code_project_roots=(),
        code_include_generated=True,
        code_include_vendored=True,
        code_complexity_warning=15,
        code_function_lines_warning=200,
        selection=CandidateSelection(),
    )
    abundant = MemorySnapshot(2**40, 2**40, 2**40, 2**40)
    with (
        patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=abundant,
        ),
        patch.object(neocortex.deduplication, "DedupIndex", FakeDedupIndex),
        patch.object(code_route_module, "CodeRoute", FakeCodeRoute),
    ):
        coordinator = _coordinator()
        result = _run_code(
            RouteExecutionContext(
                config=config,  # type: ignore[arg-type]
                root=tmp_path,
                framework_state=_FrameworkState(),  # type: ignore[arg-type]
                run_id=1,
                scan_id=1,
                progress=None,
                resource_coordinator=coordinator,
                cancellation=CancellationToken(),
            )
        )

    assert result == "coordinated"
    assert captured and captured[0] is not None
    summary = coordinator.summary()
    assert summary.routes["code"].admissions == 1
    assert summary.peak_cpu_slots == 1
    assert coordinator.route_active_request_count("code") == 0


# endregion [03]
