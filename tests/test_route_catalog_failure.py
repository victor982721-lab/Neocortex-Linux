"""Route/catalog failure separation regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.text.text_route import TextRouteSummary
from neocortex.documents import document_catalog
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.runtime.orchestration.route_registry import (
    RouteAdapter,
    RouteExecutionContext,
    _summary_with_catalog,
    _update_document_catalog_after_route,
)
from tests.test_framework_route_snapshot_concurrency import _populate


class _LifecycleProbe:
    def __init__(self) -> None:
        self.begun: list[tuple[object, ...]] = []
        self.completed: list[tuple[object, ...]] = []
        self.failed: list[tuple[object, ...]] = []
        self.events: list[tuple[object, ...]] = []

    def begin_route_phase(self, *args: object, **kwargs: object) -> None:
        self.begun.append((*args, kwargs))

    def complete_route_phase(self, *args: object, **kwargs: object) -> None:
        self.completed.append((*args, kwargs))

    def fail_route_phase(self, *args: object, **kwargs: object) -> None:
        self.failed.append((*args, kwargs))

    def record_event(self, *args: object, **kwargs: object) -> None:
        self.events.append((*args, kwargs))


def _context(tmp_path: Path, state: _LifecycleProbe) -> RouteExecutionContext:
    config = SimpleNamespace(
        document_catalog_enabled=True,
        document_catalog_database=tmp_path / "document-catalog.sqlite3",
        document_taxonomy_path=None,
        document_classification_max_chars=1024,
        resume_run_id=None,
        text_database=tmp_path / "text.sqlite3",
    )
    return RouteExecutionContext(
        config=config,  # type: ignore[arg-type]
        root=tmp_path,
        framework_state=state,  # type: ignore[arg-type]
        run_id=7,
        scan_id=11,
        progress=None,
        resource_coordinator=None,
        cancellation=CancellationToken(),
    )


def test_catalog_reader_failure_is_recorded_without_failing_producer_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _LifecycleProbe()
    failure = RuntimeError("reader detail")

    def fail_update(*args: object, **kwargs: object):
        del args, kwargs
        raise failure

    monkeypatch.setattr(document_catalog, "update_document_catalog_source", fail_update)

    summaries = _update_document_catalog_after_route(_context(tmp_path, state), "text")

    assert len(summaries) == 1
    assert summaries[0].errors == 1
    assert summaries[0].publication_state == "unavailable"
    assert state.begun and not state.completed
    assert len(state.failed) == 1
    assert state.failed[0][3] is failure
    assert state.events and "no disponible" in str(state.events[0][3])

    projected = _summary_with_catalog(TextRouteSummary(candidates=1), summaries)
    assert projected.catalog_errors == 1
    assert projected.catalog_complete is False


def test_catalog_cancellation_remains_run_control_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _LifecycleProbe()
    failure = CancellationRequested("stop catalog")

    def cancel_update(*args: object, **kwargs: object):
        del args, kwargs
        raise failure

    monkeypatch.setattr(document_catalog, "update_document_catalog_source", cancel_update)

    with pytest.raises(CancellationRequested) as raised:
        _update_document_catalog_after_route(_context(tmp_path, state), "text")
    assert raised.value is failure
    assert len(state.failed) == 1
    assert not state.completed


def test_catalog_partiality_marks_run_incomplete_without_failing_producer(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()

    def producer(_context: RouteExecutionContext) -> TextRouteSummary:
        return TextRouteSummary(candidates=1, catalog_errors=1, catalog_complete=False)

    orchestrator = FrameworkOrchestrator(
        FrameworkConfig(root=tmp_path, state_directory=state_directory, route="text"),
        route_registry={"text": RouteAdapter("text", producer)},
    )
    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id = _populate(state, tmp_path)
        stat = tmp_path.stat()
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(tmp_path),
                root_identity=(stat.st_dev, stat.st_ino, -1),
                selected_routes=("text",),
            ).event_payload(),
        )
        results, _ = orchestrator._run_content_routes(
            root=tmp_path, state=state, run_id=run_id, scan_id=1
        )
        assert "text" in results
        assert orchestrator._unavailable_routes == {
            "text:catalog": "catalog_incomplete: errors=1"
        }
        route_status = state._connection.execute(
            "SELECT status FROM route_runs WHERE run_id=? AND route_name='text'", (run_id,)
        ).fetchone()[0]
        assert route_status == "completed"


def test_catalog_cancellation_through_scheduler_is_run_cancellation(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    failure = CancellationRequested("catalog cancellation")

    def producer(_context: RouteExecutionContext) -> TextRouteSummary:
        raise failure

    orchestrator = FrameworkOrchestrator(
        FrameworkConfig(root=tmp_path, state_directory=state_directory, route="text"),
        route_registry={"text": RouteAdapter("text", producer)},
    )
    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id = _populate(state, tmp_path)
        stat = tmp_path.stat()
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(tmp_path),
                root_identity=(stat.st_dev, stat.st_ino, -1),
                selected_routes=("text",),
            ).event_payload(),
        )
        with pytest.raises(CancellationRequested) as raised:
            orchestrator._run_content_routes(
                root=tmp_path, state=state, run_id=run_id, scan_id=1
            )
    assert raised.value is failure
    assert orchestrator._cancellation.is_cancelled
