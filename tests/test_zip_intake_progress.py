"""Bounded ZIP Intake and successor-inventory progress contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from neocortex.api.cli.cli_app import _emit_unsuccessful_execution
from neocortex.progress import ProgressEvent, RecordingProgress
from neocortex.runtime.orchestration.orchestrator_pipeline import (
    _BoundedZipProgress,
    InitialPipelineMixin,
)
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.workflow import zip_intake_orchestrator
from neocortex.deduplication import FileSnapshot

if TYPE_CHECKING:
    from neocortex.deduplication import DedupIndex
    from neocortex.integrations.inventory.inventory_boundary import NormalInventoryBoundary
    from neocortex.integrations.inventory.inventory_coordinator import PreparedInventory
    from neocortex.persistence.framework_state_writer import FrameworkState


def _metrics(event: ProgressEvent) -> dict[str, int | str]:
    return {metric.name: metric.value for metric in event.metrics}


def test_zip_progress_is_bounded_and_keeps_physical_counters() -> None:
    recording = RecordingProgress()
    reporter = _BoundedZipProgress(recording, total_hint=257)

    for completed in range(1, 10_000):
        reporter(
            ProgressEvent(
                "engine",
                "member",
                "member",
                completed,
                10_000,
                "members",
                False,
            )
        )
    reporter.finish(
        {
            "status": "applied",
            "candidates": 257,
            "generic_candidates": 123,
            "atomic_packages": 134,
            "applied": 257,
            "published": 1_024,
            "members": 1_024,
            "statuses": {"applied": 257},
        }
    )

    assert len(recording.events) <= 129  # 128 live updates plus one terminal
    assert recording.events[-1].finished is True
    metrics = _metrics(recording.events[-1])
    assert metrics["generic"] == 123
    assert metrics["atomic"] == 134
    assert metrics["applied"] == 257
    assert metrics["members"] == 1_024
    assert metrics["extracted"] == 1_024
    assert recording.events[-1].description == "Procesando ZIPs"


def test_zip_progress_does_not_leave_a_task_when_no_zip_exists() -> None:
    recording = RecordingProgress()
    reporter = _BoundedZipProgress(recording, total_hint=1_000)

    reporter.finish({"status": "planned", "candidates": 0})

    assert recording.events == []


def test_successor_inventory_progress_is_distinct_and_reports_new_total() -> None:
    recording = RecordingProgress()
    owner = cast(InitialPipelineMixin, SimpleNamespace(progress=recording))

    InitialPipelineMixin._emit_zip_reconciliation_progress(
        owner,
        completed=0,
        total=1,
        source_files=144_800,
    )
    InitialPipelineMixin._emit_zip_reconciliation_progress(
        owner,
        completed=1,
        total=1,
        source_files=144_800,
        successor_files=149_459,
        successor_scan_id=7,
        status="completed",
        finished=True,
    )

    assert [event.phase for event in recording.events] == [
        "zip-intake-reconciliation",
        "zip-intake-reconciliation",
    ]
    assert recording.events[0].description == "Reconciliando inventario tras ZIP Intake"
    terminal = _metrics(recording.events[-1])
    assert terminal["source_files"] == 144_800
    assert terminal["successor_files"] == 149_459
    assert terminal["successor_scan_id"] == 7
    assert terminal["status"] == "completed"


def test_cancellation_terminal_event_mentions_committed_zip_effects() -> None:
    recording = RecordingProgress()
    failure = KeyboardInterrupt()
    failure.__dict__["zip_intake_effects"] = {
        "applied_containers": 3,
        "published_files": 17,
        "trashed_sources": 3,
    }

    _emit_unsuccessful_execution(
        recording,
        failure,
        error_code="execution_cancelled",
        errors=0,
        cancelled=True,
    )

    metrics = _metrics(recording.events[-1])
    assert "3 contenedores aplicados" in str(metrics["zip_effects"])
    assert "17 archivos físicos publicados" in str(metrics["zip_effects"])


def test_pipeline_passes_real_progress_and_cancellation_to_zip_stage(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    source = tmp_path / "source.zip"
    snapshot = FileSnapshot(str(source), 1, 2, 4, 3, 4)

    def stage(**kwargs: object):
        captured.update(kwargs)
        progress = kwargs["progress"]
        assert callable(progress)
        progress(
            ProgressEvent(
                "engine",
                "source",
                "source",
                1,
                1,
                "ZIPs",
                True,
            )
        )
        return zip_intake_orchestrator.ZipIntakeStageResult(
            {"status": "planned", "candidates": 1},
        )

    monkeypatch.setattr(zip_intake_orchestrator, "run_zip_intake_stage", stage)
    config = SimpleNamespace(
        route="all",
        route_only=False,
        apply_actions=False,
        max_file_bytes=None,
        state_directory=tmp_path / "state",
    )
    recording = RecordingProgress()
    owner = cast(
        InitialPipelineMixin,
        SimpleNamespace(
            config=config,
            progress=recording,
            _cancellation=CancellationToken(),
        ),
    )
    inventory = SimpleNamespace(scan=SimpleNamespace(scan_id=1, files_seen=1))
    state = SimpleNamespace(
        set_run_phase=lambda *_args: None,
        record_event=lambda *_args: None,
    )
    dedup_index = SimpleNamespace(snapshots=lambda _scan_id: (snapshot,))
    boundary = SimpleNamespace(verify=lambda: None)

    successor, payload = InitialPipelineMixin._run_zip_intake_stage(
        owner,
        state=cast("FrameworkState", state),
        run_id=1,
        root=tmp_path,
        boundary=cast("NormalInventoryBoundary", boundary),
        inventory=cast("PreparedInventory", inventory),
        dedup_index=cast("DedupIndex", dedup_index),
    )

    assert id(successor) == id(inventory)
    payload_status: object = payload.get("status")
    assert payload_status == "planned"
    assert captured["cancellation"] is owner._cancellation
    assert callable(captured["progress"])
    assert any(event.operation == "zip-intake" for event in recording.events)
