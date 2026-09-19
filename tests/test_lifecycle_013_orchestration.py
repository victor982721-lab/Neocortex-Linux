"""Focused orchestration contracts for the 0.13 lifecycle tranche."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any, cast

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.models import RouteOnlyRunResult
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import (
    RouteAdapter,
    RouteExecutionContext,
    builtin_route_registry,
)
from neocortex.runtime.orchestration.run_status import list_run_status
from neocortex.safety.route_filters import CandidateSelection
from tests.test_run_control import _source_run


class _CandidateView:
    candidate_database = Path("/tmp/detached-route-candidates.sqlite3")

    def __init__(self, rows: tuple[FileSnapshot, ...]) -> None:
        self.rows = rows

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: CandidateSelection,
    ):
        if mime == "application/pdf":
            yield from self.rows


def _context(
    tmp_path: Path,
    view: _CandidateView,
    *,
    max_file_bytes: int | None = None,
    max_documents: int | None = None,
) -> RouteExecutionContext:
    config = Namespace(
        selection=CandidateSelection(),
        pdf_max_file_bytes=max_file_bytes,
        pdf_max_documents=max_documents,
    )
    return RouteExecutionContext(
        config=cast(FrameworkConfig, config),
        root=tmp_path,
        framework_state=cast(Any, view),
        run_id=1,
        scan_id=1,
        progress=None,
        resource_coordinator=None,
        cancellation=cast(Any, Namespace()),
    )


def test_route_scheduler_builds_deterministic_dependency_waves(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    orchestrator = FrameworkOrchestrator(
        FrameworkConfig(root=root, state_directory=tmp_path / "state", route="audio,video"),
        route_registry={
            "audio": RouteAdapter("audio", lambda _context: {}),
            "video": RouteAdapter("video", lambda _context: {}, depends_on=("audio",)),
        },
    )

    assert orchestrator._route_execution_stages() == (("audio",), ("video",))


def test_builtin_workload_estimate_applies_mime_size_and_count_filters(tmp_path: Path) -> None:
    rows = tuple(
        FileSnapshot(str(tmp_path / name), 1, index, size, 1, 1)
        for index, (name, size) in enumerate(
            (("small.pdf", 4), ("large.pdf", 9), ("too-large.pdf", 20)),
            start=1,
        )
    )
    adapter = builtin_route_registry()["pdf"]
    assert adapter.estimate_workload is not None

    estimate = adapter.estimate_workload(
        _context(tmp_path, _CandidateView(rows), max_file_bytes=10, max_documents=1)
    )

    # The count limit selects one item; the byte bound uses the largest
    # eligible candidate so it cannot under-reserve a different route order.
    assert estimate == (1, 9)


def test_code_only_execution_does_not_materialize_route_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    state_directory = tmp_path / "state"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "module.py").write_text("def fixture():\n    return True\n", encoding="utf-8")

    def unexpected_snapshot(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Code-only execution must not create a candidate snapshot")

    monkeypatch.setattr(FrameworkState, "route_candidate_snapshot", unexpected_snapshot)
    result = FrameworkOrchestrator(
        FrameworkConfig(
            root=root,
            state_directory=state_directory,
            route="code",
            code_candidate_scope="projects",
            code_project_roots=(root,),
            global_min_free_memory_bytes=0,
            global_min_free_commit_bytes=0,
        )
    ).run_initial()

    assert result.code is not None
    assert result.code.candidates == 2


def test_semantic_only_resume_creates_no_content_route_run(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    source_run = _source_run(database, root)
    with FrameworkState(database) as state:
        state.publish_run_stage(
            source_run,
            "semantic",
            "interrupted",
            details={"selected_sources": ["text"], "image_available": False},
            idempotency_key="semantic:interrupted",
        )

    calls: list[int] = []
    result = FrameworkOrchestrator(
        FrameworkConfig(
            root=root,
            state_directory=state_directory,
            route="none",
            route_only=True,
            resume_run_id=source_run,
            heartbeat_interval_seconds=0.01,
        ),
        route_registry={
            "probe": RouteAdapter("probe", lambda context: calls.append(context.run_id)),
        },
    ).run()

    result = cast(RouteOnlyRunResult, result)
    assert result.source_run_id == source_run
    assert result.route_results == {}
    assert calls == []
    status = list_run_status(database, run_id=result.run_id, limit=1)[0]
    assert status.run_kind == "resume"
    assert status.routes == ()
