from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.api.cli.cli_parser import build_parser
from neocortex.deduplication import FileSnapshot
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.framework_state_writer import RunBudgetExceeded
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    current_resource_coordinator,
    resource_scope,
)
from neocortex.runtime.control.memory_runtime import MemorySnapshot
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import RouteAdapter, RouteExecutionContext


def test_automatic_worker_defaults_keep_explicit_ceilings() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    config = FrameworkConfig()
    assert args.image_workers is args.pdf_workers is args.ocr_workers is None
    assert config.image_workers is config.pdf_workers is config.pdf_ocr_workers is None
    assert args.pdf_large_document_workers is config.pdf_large_document_workers is None
    assert args.global_resource_wait_timeout is config.global_resource_wait_timeout_seconds is None
    explicit = parser.parse_args(["--image-workers", "11", "--pdf-workers", "13", "--ocr-workers", "7"])
    assert (explicit.image_workers, explicit.pdf_workers, explicit.ocr_workers) == (11, 13, 7)


@pytest.mark.parametrize("selectors", (
    ("image/png", "image/", "image/jpeg"),
    ("image/", "image/png", "image/jpeg"),
    ("image/png", "image/jpeg", "image/", "image/"),
))
@pytest.mark.parametrize("limit, expected", ((None, (3, 20)), (2, (2, 16))))
def test_workload_overlap_preserves_unique_items_and_largest_byte_bound(
    tmp_path: Path, monkeypatch, selectors, limit, expected,
) -> None:
    from neocortex.platform import content_capability_manifest
    from neocortex.runtime.orchestration.route_registry import _candidate_route_workload

    # Each real candidate path has exactly one MIME, even when several route
    # selectors include it. The oversized file must remain excluded.
    rows = tuple(
        (mime, FileSnapshot(str(tmp_path / name), 1, index, size, 1, -1))
        for index, (mime, name, size) in enumerate((
            ("image/png", "z.png", 4),
            ("image/png", "b.png", 7),
            ("image/jpeg", "a.jpg", 9),
            ("image/png", "large.png", 100),
        ), start=1)
    )

    class Owner:
        candidate_database = tmp_path / "candidates.sqlite3"

        def iter_selected_route_candidates(self, _run, mime, _route, _selection):
            return (snapshot for observed, snapshot in rows if observed == mime)

        def iter_selected_route_candidates_by_prefix(self, _run, mime, _route, _selection):
            return ((observed, snapshot) for observed, snapshot in rows if observed.startswith(mime))

    monkeypatch.setattr(
        content_capability_manifest, "content_capability_by_id",
        lambda _route: SimpleNamespace(mime_types=selectors),
    )
    context = RouteExecutionContext(
        config=FrameworkConfig(image_max_file_bytes=10, image_max_documents=limit),
        root=tmp_path, framework_state=Owner(), run_id=1, scan_id=1,
        progress=None, resource_coordinator=None, cancellation=CancellationToken(),
    )
    assert _candidate_route_workload(context, "image") == expected


def test_resource_wait_uses_published_deadline_without_reopening_owner(monkeypatch) -> None:
    from neocortex.runtime.orchestration import orchestrator as module

    clock = {"ns": 1_000_000_000}
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: clock["ns"])
    monkeypatch.setattr(module.time, "time_ns", lambda: 100_000_000_000)
    owner_thread = threading.get_ident()
    reads = 0

    class Owner:
        def read_run_budget(self, run_id):
            nonlocal reads
            assert threading.get_ident() == owner_thread
            assert run_id == 17
            reads += 1
            return {"deadline_ns": 102_000_000_000, "remaining_items": 20}

    orchestrator = FrameworkOrchestrator(FrameworkConfig(route="none"))
    orchestrator._bind_resource_deadline(Owner(), 17)
    calls = 0

    def pressure():
        nonlocal calls
        calls += 1
        if calls > 1:
            clock["ns"] = 4_000_000_000
        return MemorySnapshot(4096, 0, 4096, 0)

    coordinator = GlobalResourceCoordinator(
        ("inventory",),
        GlobalResourceLimits(
            cpu_slots=1, memory_budget_bytes=4096,
            min_free_memory_bytes=0, min_free_commit_bytes=0,
            wait_timeout_seconds=None, poll_interval_seconds=0.001,
        ),
        cpu_load_probe=lambda: 0.0, resource_probe=pressure,
        checkpoint=orchestrator._check_resource_deadline,
    )
    with pytest.raises(RunBudgetExceeded, match="time"):
        with coordinator.admit("inventory", 512):
            pytest.fail("resource wait crossed the published run deadline")
    assert reads == 1
    summary = coordinator.summary()
    assert summary.transient_bytes == summary.resident_bytes == summary.cpu_slots_in_use == 0






def test_video_can_consume_published_audio_while_catalog_is_pending(tmp_path: Path) -> None:
    state_path = tmp_path / "state"
    state_path.mkdir()
    consumer_started = threading.Event()
    observed: list[object] = []

    def audio(context):
        observed.append(current_resource_coordinator())
        assert context.source_published is not None
        context.source_published("audio")
        assert consumer_started.wait(5), "video waited for the independent audio catalog phase"
        return {"processed": 1}

    def video(context):
        observed.append(current_resource_coordinator())
        consumer_started.set()
        return {"processed": 1}

    config = FrameworkConfig(root=tmp_path, state_directory=state_path, route="all")
    orchestrator = FrameworkOrchestrator(config, route_registry={
        "audio": RouteAdapter("audio", audio),
        "video": RouteAdapter("video", video, depends_on=("audio",)),
    })
    with FrameworkState(config.framework_database) as state:
        run_id = state.begin_initial_run(tmp_path, None)
        state.publish_initial_routing_snapshot(run_id, 1, 0, 1, "full", 0)
        with orchestrator._run_resource_scope() as coordinator:
            results, _ = orchestrator._run_content_routes(
                root=tmp_path, state=state, run_id=run_id, scan_id=1,
            )
            assert set(results) == {"audio", "video"}
            assert observed == [coordinator, coordinator]
            assert orchestrator._active_coordinator is coordinator
    assert orchestrator._active_coordinator is None
