"""Integration regressions for scoped replay, ready scheduling and recovery."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.documents import document_catalog as catalog
from neocortex.documents import document_organization as organization
from neocortex.documents import document_organization_planning as planning
from neocortex.documents.document_organization_recovery import OrganizationRecoveryRequired
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
)
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration import orchestrator as orchestrator_module
from neocortex.runtime.orchestration.organization_lifecycle import (
    organization_stage_state,
    register_organization_stages,
)
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator, build_normal_inventory_boundary
from neocortex.runtime.orchestration.route_registry import (
    RouteAdapter,
    RouteExecutionContext,
    _update_document_catalog_after_route,
)
from tests.test_document_catalog_generation_publication import _upsert_docx_source
from tests.test_framework_route_snapshot_concurrency import _populate


@pytest.fixture
def controlled_resources(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep real run-scoped admission and cancellation with stable capacity."""

    capacity = 1024 * 1024 * 1024
    sample = ResourceSample(
        available_physical=2 * capacity, available_commit=2 * capacity,
        total_physical=2 * capacity, total_commit=2 * capacity,
        cpu_load_percent=0, external_cpu_cores=0, effective_cpu_capacity=4,
        memory_pressure_some_percent=0, memory_pressure_full_percent=0,
        io_pressure_some_percent=0, io_pressure_full_percent=0,
    )
    coordinators: list[GlobalResourceCoordinator] = []

    def create(
        route_order: tuple[str, ...], limits: GlobalResourceLimits, *,
        cancellation: CancellationToken, checkpoint: Callable[[], None],
        route_memory_budgets: Mapping[str, int],
    ) -> GlobalResourceCoordinator:
        coordinator = GlobalResourceCoordinator(
            route_order,
            replace(
                limits, memory_budget_bytes=capacity, temp_budget_bytes=capacity,
                min_free_memory_bytes=0, min_free_commit_bytes=0,
                cpu_slots=4, native_thread_slots=4, max_cpu_load_percent=100,
                wait_timeout_seconds=2, poll_interval_seconds=0.01,
            ),
            cpu_load_probe=lambda: 0.0, effective_cpu_probe=lambda: 4,
            resource_probe=lambda: sample,
            cancellation=cancellation, checkpoint=checkpoint,
            route_memory_budgets=route_memory_budgets,
        )
        coordinators.append(coordinator)
        return coordinator

    # Preserve the orchestrator's active-scope reuse, stage registration and
    # cancellation token; only replace the constructor's environmental inputs.
    monkeypatch.setattr(orchestrator_module, "GlobalResourceCoordinator", create)
    try:
        yield
        assert coordinators
        for coordinator in coordinators:
            summary = coordinator.summary()
            assert summary.resident_bytes == summary.transient_bytes == summary.temp_bytes == 0
            assert summary.cpu_slots_in_use == summary.native_threads == 0
            assert all(coordinator.route_active_request_count(route) == 0 for route in summary.routes)
    finally:
        for coordinator in coordinators:
            coordinator.close()


def _sources(tmp_path: Path, count: int = 1) -> tuple[FrameworkConfig, list[Path]]:
    root = tmp_path / "corpus"
    root.mkdir()
    config = FrameworkConfig(
        root=root, state_directory=tmp_path / "state", route="text",
        organization_root=tmp_path / "organized", heartbeat_interval_seconds=0.01,
        global_min_free_memory_bytes=0, global_min_free_commit_bytes=0,
    )
    config.state_directory.mkdir()
    paths = []
    for number in range(count):
        source = root / f"document-{number:04d}.docx"
        source.write_bytes(f"fixture-{number}".encode())
        _upsert_docx_source(
            config.docx_database, source, title="IEEE C37.20.2",
            text="IEEE switchgear standard", signature="stable",
        )
        paths.append(source)
    return config, paths


def _update(config: FrameworkConfig, **kwargs: object):
    return catalog.update_document_catalog_source(
        config.document_catalog_database, config.docx_database, "docx",
        source_root=config.root, verify_source_paths=False, **kwargs,
    )


def _publication_counts(config: FrameworkConfig) -> tuple[int, int, int]:
    with catalog.document_catalog_database(config.document_catalog_database, readonly=True) as db:
        return tuple(
            int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("catalog_generations", "catalog_generation_documents", "classification_history")
        )


def test_scoped_route_replay_preserves_other_root_and_original_producer(tmp_path: Path) -> None:
    config, paths = _sources(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = other_root / "other.docx"
    other.write_bytes(b"other")
    _upsert_docx_source(
        config.docx_database, other, title="IEEE C37.20.2",
        text="IEEE switchgear standard", signature="stable",
    )
    _update(replace(config, root=other_root))
    context = RouteExecutionContext(
        config=config, root=config.root, framework_state=SimpleNamespace(record_event=lambda *args: None),
        run_id=2, scan_id=1, progress=None, resource_coordinator=None,
        cancellation=CancellationToken(),
    )
    first = _update_document_catalog_after_route(context, "docx")[0]
    before = _publication_counts(config)
    with catalog.document_catalog_database(config.document_catalog_database, readonly=True) as db:
        manifest = catalog.read_catalog_publication_manifest(db, "docx")
    assert first.candidates == 1
    assert manifest.source_root == str(config.root)
    for _ in range(2):
        replay = _update_document_catalog_after_route(context, "docx")[0]
        assert replay.publication_state == "unchanged"
        assert replay.generation_id == first.generation_id
        assert replay.reused_from_catalog_run_id == first.catalog_run_id
        assert (replay.candidates, replay.cache_hits, replay.classified) == (1, 1, 0)
        assert _publication_counts(config) == before
    assert {Path(document.path) for document in catalog.list_catalog_documents(config.document_catalog_database, limit=100)} == {*paths, other}

    # A producer observation may update operational fields while all exact
    # classification inputs remain identical. Its fresh fence belongs to the
    # observation; the original producer manifest remains byte-for-byte equal.
    with sqlite3.connect(config.docx_database) as db:
        db.execute("UPDATE documents SET last_seen_run_id=last_seen_run_id+1")
    replay = _update(config)
    assert replay.publication_state == "unchanged"
    with catalog.document_catalog_database(config.document_catalog_database, readonly=True) as db:
        assert catalog.read_catalog_publication_manifest(db, "docx") == manifest
        validated = catalog.validate_catalog_publication_scope(db, "docx", config.root)
        assert validated == manifest
    assert _publication_counts(config) == before


def test_one_changed_input_retains_127_cached_classifications(tmp_path: Path) -> None:
    config, _ = _sources(tmp_path, 128)
    assert _update(config).classified == 128
    assert _update(config).publication_state == "unchanged"
    # Title is consumed by classification but not by the old text fingerprint.
    with sqlite3.connect(config.docx_database) as db:
        db.execute("UPDATE documents SET title='Invoice for transformer' WHERE path=(SELECT MIN(path) FROM documents)")
    delta = _update(config)
    assert (delta.candidates, delta.classified, delta.cache_hits) == (128, 1, 127)
    assert delta.publication_state == "published"
    assert _update(config).publication_state == "unchanged"
    bounded = _update(config, max_text_chars=128)
    assert (bounded.classified, bounded.cache_hits) == (128, 0)
    assert _update(config, max_text_chars=128).publication_state == "unchanged"


def test_cancellation_during_replay_sql_preserves_projection_and_releases_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = _sources(tmp_path, 128)
    _update(config)
    before = _publication_counts(config)
    inside_projection = False
    failure = CancellationRequested("cancel inside projection SQL")
    sql_checkpoints = 0

    class Token(CancellationToken):
        def checkpoint(self) -> None:
            nonlocal sql_checkpoints
            if inside_projection:
                sql_checkpoints += 1
                raise failure
            super().checkpoint()

    original = catalog.current_projection_matches

    def observe(*args: object):
        nonlocal inside_projection
        inside_projection = True
        try:
            return original(*args)
        finally:
            inside_projection = False

    monkeypatch.setattr(catalog, "current_projection_matches", observe)
    with pytest.raises(CancellationRequested) as raised:
        _update(config, cancellation=Token())
    assert raised.value is failure
    assert sql_checkpoints >= 1
    assert _publication_counts(config) == before
    assert _update(config).publication_state == "unchanged"


def test_cancellation_during_replay_source_iteration_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = _sources(tmp_path, 12)
    _update(config)
    before = _publication_counts(config)
    token = CancellationToken()
    original = catalog._iter_source_documents
    visited = 0

    def rows(*args: object, **kwargs: object):
        nonlocal visited
        for document in original(*args, **kwargs):
            visited += 1
            if visited == 3:
                token.cancel()
            yield document

    monkeypatch.setattr(catalog, "_iter_source_documents", rows)
    with pytest.raises(CancellationRequested):
        _update(config, cancellation=token)
    assert visited == 3
    assert _publication_counts(config) == before


def test_replay_source_snapshot_preparation_is_cancellable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neocortex.persistence import sqlite_immutable

    config, _ = _sources(tmp_path)
    _update(config)
    before = _publication_counts(config)
    copying_source = False
    failure = CancellationRequested("cancel during source snapshot copy")
    copy_checkpoints = 0

    class Token(CancellationToken):
        def checkpoint(self) -> None:
            nonlocal copy_checkpoints
            if copying_source:
                copy_checkpoints += 1
                raise failure
            super().checkpoint()

    original = sqlite_immutable._copy_regular_file

    def copy(source: Path, destination: Path, **kwargs: object) -> None:
        nonlocal copying_source
        copying_source = source == config.docx_database
        try:
            original(source, destination, **kwargs)
        finally:
            copying_source = False

    monkeypatch.setattr(catalog, "preferred_sqlite_read_mode", lambda _path: sqlite_immutable.SQLiteReadMode.SNAPSHOT_TEMP)
    monkeypatch.setattr(sqlite_immutable, "_copy_regular_file", copy)
    with pytest.raises(CancellationRequested) as raised:
        _update(config, cancellation=Token())
    assert raised.value is failure
    assert copy_checkpoints >= 1
    assert _publication_counts(config) == before


@pytest.mark.parametrize("audio_unavailable", (False, True))
@pytest.mark.usefixtures("controlled_resources")
def test_ready_video_starts_while_unrelated_text_is_running(
    tmp_path: Path, audio_unavailable: bool,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    video_started = threading.Event()
    text_observations: list[bool] = []

    class AudioUnavailable(RuntimeError):
        capability_unavailable = True

    def audio(_context):
        if audio_unavailable:
            raise AudioUnavailable("fixture")
        return {}

    def text(_context):
        text_observations.append(video_started.wait(2))
        return {}

    def video(_context):
        video_started.set()
        return {}

    orchestrator = FrameworkOrchestrator(
        FrameworkConfig(root=tmp_path, state_directory=state_directory, route="all"),
        route_registry={
            "text": RouteAdapter("text", text), "audio": RouteAdapter("audio", audio),
            "video": RouteAdapter("video", video, depends_on=("audio",)),
        },
    )
    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id = _populate(state, tmp_path)
        state.publish_run_manifest(run_id, RunManifest(
            run_id=run_id, run_kind="initial", root=str(tmp_path),
            root_identity=(tmp_path.stat().st_dev, tmp_path.stat().st_ino, -1),
            selected_routes=("text", "audio", "video"),
        ).event_payload())
        results, _ = orchestrator._run_content_routes(root=tmp_path, state=state, run_id=run_id, scan_id=1)
    assert text_observations == [True]
    assert "video" in results
    assert ("audio" in results) is not audio_unavailable


@pytest.mark.usefixtures("controlled_resources")
def test_ready_child_reserves_budget_before_submission(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    child_calls: list[bool] = []
    text_finished = threading.Event()

    def text(context):
        assert context.cancellation.wait(2)
        text_finished.set()
        return {}

    orchestrator = FrameworkOrchestrator(
        FrameworkConfig(root=tmp_path, state_directory=state_directory, route="all"),
        route_registry={
            "text": RouteAdapter("text", text),
            "audio": RouteAdapter("audio", lambda _context: {}),
            "video": RouteAdapter("video", lambda _context: child_calls.append(True) or {}, depends_on=("audio",)),
        },
    )
    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id = _populate(state, tmp_path)
        state.publish_run_manifest(run_id, RunManifest(
            run_id=run_id, run_kind="initial", root=str(tmp_path),
            root_identity=(tmp_path.stat().st_dev, tmp_path.stat().st_ino, -1),
            selected_routes=("text", "audio", "video"), budget={"max_items": 12},
        ).event_payload())
        with pytest.raises(RunBudgetExceeded):
            orchestrator._run_content_routes(root=tmp_path, state=state, run_id=run_id, scan_id=1)
        assert state.read_run_budget(run_id)["consumed_items"] == 12
    assert child_calls == []
    assert text_finished.is_set()


@pytest.mark.parametrize("after_owner_commit", (False, True))
@pytest.mark.usefixtures("controlled_resources")
def test_initial_organization_interruption_resumes_without_content_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_owner_commit: bool,
) -> None:
    config, _ = _sources(tmp_path)
    _update(config)
    content_calls: list[int] = []
    semantic_calls: list[int] = []
    registry = {"text": RouteAdapter("text", lambda context: content_calls.append(context.run_id) or {})}
    planner = organization.plan_document_organization

    def interrupted(*args: object, **kwargs: object):
        if after_owner_commit:
            planner(*args, **kwargs)
        raise KeyboardInterrupt("injected organization interruption")

    monkeypatch.setattr(organization, "plan_document_organization", interrupted)
    initial = FrameworkOrchestrator(config, route_registry=registry, lifecycle_stage_runner=semantic_calls.append)
    with pytest.raises(KeyboardInterrupt):
        initial.run_initial()
    assert len(content_calls) == 1
    source_run = content_calls[0]
    with FrameworkState(config.framework_database) as state:
        assert organization_stage_state(state, source_run)["organization_plan"]["status"] == "interrupted"
        assert state.run_recovery_plan(source_run)["pending_stages"] == ["organization_plan"]
    assert semantic_calls == []
    monkeypatch.setattr(organization, "plan_document_organization", planner)

    original_publish = FrameworkState.publish_run_stage

    def publish_after_charge(state, run_id: int, stage: str, status: str, **kwargs: object):
        if stage == "organization_plan" and status == "completed":
            assert state.read_run_stage_budget(run_id)["organization_plan"]["items"] == 1
        return original_publish(state, run_id, stage, status, **kwargs)

    monkeypatch.setattr(FrameworkState, "publish_run_stage", publish_after_charge)

    def semantic(run_id: int) -> None:
        with FrameworkState(config.framework_database) as state:
            assert organization_stage_state(state, run_id)["organization_plan"]["status"] == "completed"
            state.publish_run_stage(run_id, "semantic", "completed")
        semantic_calls.append(run_id)

    result = FrameworkOrchestrator(
        replace(config, route="none", route_only=True, resume_run_id=source_run),
        route_registry=registry, lifecycle_stage_runner=semantic,
    ).run()
    assert result.route_results == {}
    assert content_calls == [source_run]
    assert semantic_calls == [result.run_id]
    with FrameworkState(config.framework_database) as state:
        assert organization_stage_state(state, result.run_id)["organization_plan"]["status"] == "completed"
    with catalog.document_catalog_database(config.document_catalog_database, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM organization_plans WHERE status<>'superseded'").fetchone()[0] == 1


def test_organization_cancel_during_preparation_precedes_first_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = _sources(tmp_path, 12)
    _update(config)
    scope = organization.capture_organization_input_scope(config.document_catalog_database, config.root)
    token = CancellationToken()
    original = planning.assess_organization_resource
    assessed = 0
    progress: list[object] = []

    def assess(*args: object, **kwargs: object):
        nonlocal assessed
        assessed += 1
        result = original(*args, **kwargs)
        token.cancel()
        return result

    monkeypatch.setattr(planning, "assess_organization_resource", assess)
    with pytest.raises(CancellationRequested):
        planning.plan_document_organization(
            config.document_catalog_database, config.organization_root,
            source_scope=scope, cancellation=token, progress=progress.append,
        )
    assert assessed == 1
    assert progress == []
    with catalog.document_catalog_database(config.document_catalog_database, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM organization_plans").fetchone()[0] == 0


@pytest.mark.parametrize("owner_change", ("generation", "missing"))
@pytest.mark.usefixtures("controlled_resources")
def test_interrupted_preparation_cannot_silently_adopt_changed_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_change: str,
) -> None:
    config, _ = _sources(tmp_path)
    _update(config)
    calls: list[int] = []
    registry = {"text": RouteAdapter("text", lambda context: calls.append(context.run_id) or {})}
    planner = organization.plan_document_organization

    def interrupted(*_args: object, **_kwargs: object):
        raise KeyboardInterrupt("after durable preparation")

    monkeypatch.setattr(organization, "plan_document_organization", interrupted)
    with pytest.raises(KeyboardInterrupt):
        FrameworkOrchestrator(config, route_registry=registry).run_initial()
    source_run = calls[0]
    if owner_change == "generation":
        with sqlite3.connect(config.docx_database) as db:
            db.execute("UPDATE documents SET title='Changed classification input'")
        _update(config)
    else:
        config.document_catalog_database.rename(tmp_path / "missing-catalog.sqlite3")
    monkeypatch.setattr(organization, "plan_document_organization", planner)
    with pytest.raises((OrganizationRecoveryRequired, ValueError)):
        FrameworkOrchestrator(
            replace(config, route="none", route_only=True, resume_run_id=source_run),
            route_registry=registry,
        ).run()
    assert calls == [source_run]
    with FrameworkState(config.framework_database) as state:
        resumed_run = int(state._connection.execute("SELECT MAX(run_id) FROM initial_runs").fetchone()[0])
        assert organization_stage_state(state, resumed_run)["organization_plan"]["status"] not in {"completed", "skipped"}
        assert state._connection.execute("SELECT status FROM initial_runs WHERE run_id=?", (resumed_run,)).fetchone()[0] != "completed"


@pytest.mark.parametrize("uncertain", (False, True))
def test_resume_observes_pending_apply_without_repeating_effect_or_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: bool,
) -> None:
    config, paths = _sources(tmp_path)
    _update(config)
    registry = {"text": RouteAdapter("text", lambda _context: pytest.fail("completed content repeated"))}
    orchestrator = FrameworkOrchestrator(config, route_registry=registry)
    with FrameworkState(config.framework_database) as state:
        boundary = build_normal_inventory_boundary(config.root, config.state_directory)
        source_run = state.begin_initial_run(config.root, None, inventory_policy_signature=boundary.effective_signature)
        st = config.root.stat()
        state.publish_run_manifest(source_run, RunManifest(
            run_id=source_run, run_kind="initial", root=str(config.root),
            root_identity=(st.st_dev, st.st_ino, -1), selected_routes=(),
        ).event_payload())
        register_organization_stages(state, source_run, replace(config, apply_actions=True), config.root)
        with pytest.raises(OrganizationRecoveryRequired, match="authority"):
            orchestrator._run_document_organization(root=config.root, state=state, run_id=source_run)
        assert state.read_run_stage_budget(source_run)["organization_plan"]["items"] == 1
        assert organization_stage_state(state, source_run)["organization_plan"]["status"] == "completed"
        state.fail_initial_run(source_run)
    if uncertain:
        with catalog.document_catalog_database(config.document_catalog_database) as db:
            db.execute("UPDATE organization_plans SET status='applying'")
            db.commit()
    monkeypatch.setattr(organization, "plan_document_organization", lambda *_args, **_kwargs: pytest.fail("completed plan repeated"))
    monkeypatch.setattr(organization, "apply_all_document_organization", lambda *_args, **_kwargs: pytest.fail("effect repeated without authority"))
    with pytest.raises(OrganizationRecoveryRequired, match="reconciliation" if uncertain else "authority"):
        FrameworkOrchestrator(
            replace(config, route="none", route_only=True, resume_run_id=source_run),
            route_registry=registry,
        ).run()
    assert all(path.is_file() for path in paths)
    assert not config.organization_root.exists()
    with FrameworkState(config.framework_database) as state:
        resumed_run = int(state._connection.execute("SELECT MAX(run_id) FROM initial_runs").fetchone()[0])
        assert organization_stage_state(state, resumed_run)["organization_apply"]["status"] == "partial"


@pytest.mark.parametrize("corruption", ("owner_receipt", "summary", "destination"))
def test_completed_plan_checkpoint_revalidates_owner_and_bound_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str,
) -> None:
    from neocortex.documents.document_organization_recovery import (
        capture_organization_checkpoint,
        inspect_organization_checkpoint,
    )

    config, _ = _sources(tmp_path)
    _update(config)
    scope = organization.capture_organization_input_scope(config.document_catalog_database, config.root)
    summary = organization.plan_document_organization(
        config.document_catalog_database, config.organization_root, source_scope=scope,
    )
    checkpoint = capture_organization_checkpoint(config.document_catalog_database, scope, config.organization_root, summary)
    if corruption == "owner_receipt":
        with catalog.document_catalog_database(config.document_catalog_database) as db:
            db.execute("UPDATE catalog_runs SET status='failed' WHERE catalog_run_id=?", (summary.catalog_run_id,))
            db.commit()
    elif corruption == "summary":
        checkpoint["summary"] = {**checkpoint["summary"], "considered": 999}
    else:
        checkpoint["organization_root"] = str(tmp_path / "different-destination")
    with pytest.raises(OrganizationRecoveryRequired):
        inspect_organization_checkpoint(config.document_catalog_database, checkpoint, config.root, config.organization_root)


@pytest.mark.parametrize("corruption", ("checksum", "producer"))
def test_corrupt_replay_receipt_abstains_without_advancing_publication(tmp_path: Path, corruption: str) -> None:
    from neocortex.documents.document_catalog_replay import CatalogReplayReceipt

    config, _ = _sources(tmp_path)
    published = _update(config)
    before = _publication_counts(config)
    with catalog.document_catalog_database(config.document_catalog_database) as db:
        raw = json.loads(db.execute("SELECT summary_json FROM catalog_runs WHERE catalog_run_id=?", (published.catalog_run_id,)).fetchone()[0])
        if corruption == "checksum":
            raw["publication_observation"]["input_count"] = 123
        else:
            receipt = CatalogReplayReceipt.from_payload(raw["publication_observation"])
            raw["publication_observation"] = replace(receipt, producer_catalog_run_id=123).payload()
        db.execute("UPDATE catalog_runs SET summary_json=? WHERE catalog_run_id=?", (json.dumps(raw), published.catalog_run_id))
        db.commit()
    with pytest.raises(catalog.CatalogPublicationConflict, match="observation"):
        _update(config)
    assert _publication_counts(config) == before
