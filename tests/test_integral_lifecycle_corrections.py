"""Complementary public lifecycle checks for archive effects and route resume.

These are intentionally smaller than the installed eight-route E2E: they use
private HOME/XDG roots, a fixture trash backend, and synthetic route adapters
to exercise the public orchestrator boundaries without KIO or real corpus
effects.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, cast
from unittest.mock import patch

import pytest

from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.runtime.models import FrameworkConfig, RouteOnlyRunResult
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.runtime.orchestration.run_status import list_run_status
from neocortex.safety.route_filters import CandidateSelection
from tests.test_run_control import _source_run


def _private_environment(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    home = root / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("TMPDIR", str(root / "tmp"))
    (root / "tmp").mkdir(parents=True)


def test_archive_apply_replay_uses_fixture_trash_and_single_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ZIP intake publishes once, preserves the original, and replays routes."""

    _private_environment(monkeypatch, tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    fixture_trash = tmp_path / "fixture-trash"
    fixture_trash.mkdir()

    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("document.txt", b"nested text evidence\n")
    source = corpus / "bundle.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("folder/inner.zip", nested.getvalue())
        archive.writestr("root.txt", b"root text evidence\n")
    original = source.read_bytes()
    expected = {
        corpus / "bundle" / "root.txt": b"root text evidence\n",
        corpus / "bundle" / "folder" / "inner" / "document.txt": b"nested text evidence\n",
    }

    effects: list[Path] = []
    seen_candidates: list[set[Path]] = []

    class FixtureTrash:
        def apply_snapshot(self, snapshot, *, root, source_digest):
            path = Path(snapshot.path)
            assert path == source
            assert source_digest and path.is_relative_to(root)
            for materialized, payload in expected.items():
                assert materialized.read_bytes() == payload
            target = fixture_trash / path.name
            path.replace(target)
            effects.append(path)
            return SimpleNamespace(status="applied", detail="fixture-trash", receipt_json="{}")

    def consume(context):
        candidates = {
            Path(candidate.path)
            for candidate in context.framework_state.iter_selected_route_candidates(
                context.run_id, "text/plain", "text", CandidateSelection()
            )
        }
        seen_candidates.append(candidates)
        return {"processed": len(candidates)}

    config = FrameworkConfig(
        root=corpus,
        state_directory=tmp_path / "state",
        route="all",
        apply_actions=True,
        document_catalog_enabled=False,
        global_cpu_slots=2,
        global_min_free_memory_bytes=0,
        global_min_free_commit_bytes=0,
    )
    registry = {"text": RouteAdapter("text", consume)}
    with (
        patch("neocortex.workflow.mutations.KioTrashBackend", FixtureTrash),
        patch(
            "neocortex.safety.kio_trash.preflight_kio_trash",
            lambda: SimpleNamespace(client="fixture-trash"),
        ),
        patch(
            "neocortex.platform.sqlite_runtime_attestation.observe_platform_native_runtime",
            lambda **_kwargs: {"status": "approved", "observed": {}},
        ),
    ):
        first = FrameworkOrchestrator(config, route_registry=registry).run()
        second = FrameworkOrchestrator(config, route_registry=registry).run()

    first_routes = cast(Mapping[str, Mapping[str, object]], first.route_results)
    second_routes = cast(Mapping[str, Mapping[str, object]], second.route_results)
    assert first_routes["text"]["processed"] == 2
    assert second_routes["text"]["processed"] == 2
    assert seen_candidates == [set(expected), set(expected)]
    assert effects == [source]
    assert not source.exists()
    assert (fixture_trash / source.name).read_bytes() == original
    for materialized, payload in expected.items():
        assert materialized.read_bytes() == payload
    # No second extraction may create a sibling materialization or overwrite a
    # preserved source backup.
    assert sorted(path.relative_to(corpus).as_posix() for path in corpus.rglob("*")) == [
        "bundle",
        "bundle/folder",
        "bundle/folder/inner",
        "bundle/folder/inner/document.txt",
        "bundle/root.txt",
    ]


def test_public_route_cancellation_then_resume_preserves_candidate_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled route-only run remains replayable through the public API."""

    _private_environment(monkeypatch, tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source_file = corpus / "one.pdf"
    source_file.write_bytes(b"synthetic route candidate\n")
    database = tmp_path / "state" / "framework.sqlite3"
    database.parent.mkdir()
    source_run = _source_run(database, corpus, route_running=True)
    observed: list[int] = []

    def cancel_route(context):
        observed.append(context.run_id)
        list(
            context.framework_state.iter_selected_route_candidates(
                context.run_id, "application/pdf", "probe", CandidateSelection()
            )
        )
        context.cancellation.cancel()
        raise CancellationRequested("fixture cancellation")

    cancelled_config = FrameworkConfig(
        root=corpus,
        state_directory=database.parent,
        # Let route-only recovery resolve the durable manifest selection. This
        # reproduces the CLI ``--resume-run`` path rather than supplying a
        # fresh route list.
        route="none",
        route_only=True,
        resume_run_id=source_run,
        global_cpu_slots=1,
        global_min_free_memory_bytes=0,
        global_min_free_commit_bytes=0,
    )
    with pytest.raises(KeyboardInterrupt):
        FrameworkOrchestrator(
            cancelled_config,
            route_registry={"probe": RouteAdapter("probe", cancel_route)},
        ).run()

    def resume_route(context):
        candidates = list(
            context.framework_state.iter_selected_route_candidates(
                context.run_id, "application/pdf", "probe", CandidateSelection()
            )
        )
        assert [Path(candidate.path) for candidate in candidates] == [source_file]
        return {"processed": len(candidates)}

    resumed = cast(RouteOnlyRunResult, FrameworkOrchestrator(
        cancelled_config,
        route_registry={"probe": RouteAdapter("probe", resume_route)},
    ).run())

    resumed_routes = cast(Mapping[str, Mapping[str, object]], resumed.route_results)
    assert resumed_routes["probe"]["processed"] == 1
    assert resumed.global_resources is not None
    assert "probe" in resumed.global_resources.routes
    assert len(observed) == 1
    with FrameworkState(database) as state:
        statuses = [
            row[0]
            for row in state._connection.execute(
                "SELECT status FROM initial_runs WHERE run_id IN (?,?) ORDER BY run_id",
                (resumed.run_id, resumed.source_run_id),
            ).fetchall()
        ]
    assert statuses == ["interrupted", "completed"]
    listed = list_run_status(database, run_id=resumed.run_id, limit=1)
    assert listed[0].status == "completed"
