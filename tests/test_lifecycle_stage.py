"""Durable cross-owner lifecycle stage regressions."""

from __future__ import annotations

from argparse import Namespace
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.api.cli.cli_semantic import (
    _begin_integrated_publication,
    _recover_pending_integrated_publication,
    _semantic_resume_args,
    run_integrated_all_semantic_index,
)
from neocortex.api.cli import cli_app, cli_reporting, cli_semantic
from neocortex.deduplication import DedupIndex, FileSnapshot, InventoryCheckpoint
from neocortex.enumeration import JournalCursor
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.state_publication import (
    begin_state_publication,
    read_state_publication_state,
    record_state_publication,
)
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.runtime.orchestration.route_registry import builtin_route_registry
from neocortex.runtime.orchestration.orchestrator import build_normal_inventory_boundary
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.runtime.orchestration.run_status import list_run_status
from neocortex.workflow.actions.corpus_admission import CorpusAdmissionPolicy


def _manifest(
    run_id: int,
    root: Path,
    *,
    selected_routes: tuple[str, ...] = ("text",),
) -> dict[str, object]:
    config = FrameworkConfig()
    policy = CorpusAdmissionPolicy(
        interested_roots=config.code_project_roots,
        code_scope=config.code_candidate_scope,
    )
    return RunManifest(
        run_id=run_id,
        run_kind="initial",
        root=str(root),
        root_identity=(1, 2, -1),
        selected_routes=selected_routes,
        route_capabilities=dict.fromkeys(selected_routes, "safe_replay"),
        configuration={
            "corpus_admission": policy.to_dict(),
            "corpus_admission_signature": policy.signature,
        },
    ).event_payload()


def test_lifecycle_stage_is_idempotent_and_exposed_in_status(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))
        details = {"semantic_database": str(tmp_path / "semantic.sqlite3")}
        assert state.publish_run_stage(
            run_id,
            "semantic",
            "running",
            details=details,
            idempotency_key="semantic:started",
        )
        assert not state.publish_run_stage(
            run_id,
            "semantic",
            "running",
            details=details,
            idempotency_key="semantic:started",
        )
        assert state.publish_run_stage(
            run_id,
            "semantic",
            "completed",
            details={**details, "semantic_exit_code": 0},
            idempotency_key="semantic:terminal",
        )
        stages = state.read_run_stages(run_id)
        assert [stage["status"] for stage in stages] == ["running", "completed"]

    status = list_run_status(database, run_id=run_id)[0]
    assert [stage["stage"] for stage in status.stages] == ["semantic", "semantic"]
    assert status.stages[-1]["status"] == "completed"
    assert status.manifest is not None
    assert status.stages[-1]["manifest_digest"] == status.manifest["digest"]


def test_abrupt_termination_marks_running_stage_interrupted_once(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))
        state.publish_run_stage(run_id, "semantic", "running", idempotency_key="semantic:started")
        assert state.mark_abandoned_runs() == 1
        assert state.mark_abandoned_runs() == 0
        stages = state.read_run_stages(run_id)
        assert [stage["status"] for stage in stages] == ["running", "interrupted"]
        assert stages[-1]["details"]["reason"] == "abrupt_termination"

    status = list_run_status(database, run_id=run_id)[0]
    assert status.status == "interrupted"
    assert status.stages[-1]["status"] == "interrupted"


def test_abrupt_semantic_stage_after_completed_framework_run_is_recovered(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))
        state.publish_run_stage(run_id, "semantic", "running", idempotency_key="semantic:started")
        state._connection.execute(
            "UPDATE initial_runs SET status='completed',current_phase='completed',completed_ns=? WHERE run_id=?",
            (1, run_id),
        )
        assert state.mark_abandoned_runs() == 1
        assert state.mark_abandoned_runs() == 0
        assert state.read_run_stages(run_id)[-1]["status"] == "interrupted"


def test_stage_manifest_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        manifest = _manifest(run_id, root)
        state.publish_run_manifest(run_id, manifest)
        with state._connection:
            state._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,?,?,?,?)""",
                (
                    run_id,
                    2,
                    "info",
                    "lifecycle-stage",
                    "Lifecycle stage transitioned",
                    json.dumps(
                        {
                            "schema": "neocortex.lifecycle-stage/v1",
                            "run_id": run_id,
                            "manifest_digest": "sha256:" + "0" * 64,
                            "stage": "semantic",
                            "status": "running",
                            "details": {},
                            "idempotency_key": "bad",
                        },
                        separators=(",", ":"),
                    ),
                ),
            )

    with pytest.raises(sqlite3.DatabaseError, match="detached"):
        list_run_status(database, run_id=run_id)


def test_initial_route_input_sources_survive_recovery(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))
        state.publish_initial_routing_snapshot(run_id, 1, 0, 1, "full", 0)
        state.begin_route_runs(
            run_id,
            ("code",),
            route_input_sources={"code": "inventory_snapshot"},
        )
        state.mark_abandoned_runs()
        recovery = state.run_recovery_plan(run_id)
        assert recovery["route_input_sources"] == {"code": "inventory_snapshot"}
        assert recovery["non_replayable"] == []


def test_begin_operational_run_rejects_live_source(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        source_run = state.begin_initial_run(root, None)
        with pytest.raises(ValueError, match="still running"):
            state.begin_operational_run(
                root,
                run_kind="resume",
                source_run_id=source_run,
            )


def test_builtin_routes_declare_replay_capabilities() -> None:
    registry = builtin_route_registry()
    assert registry["pdf"].lifecycle_capability == "phase_resume"
    assert all(
        registry[name].lifecycle_capability in {"phase_resume", "safe_replay", "not_resumable"}
        for name in registry
    )


def test_two_recovery_attempts_keep_24_route_inputs_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    for item_index in range(24):
        (root / f"item-{item_index:02d}.pdf").write_bytes(b"%PDF-1.4\n")

    boundary = build_normal_inventory_boundary(
        root,
        state_directory,
        observe_regenerable_artifacts=True,
    )
    with DedupIndex(state_directory / "dedup.sqlite3") as index:
        scan = index.scan(root, exclusion_policy=boundary.exclusion_policy)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(
                str(root),
                scan.scan_id,
                "C:",
                1,
                11,
                True,
                boundary.exclusion_policy.signature,
            )
        )
    candidates = []
    for path in sorted(root.iterdir()):
        metadata = path.stat()
        candidates.append(
            (
                "application/pdf",
                FileSnapshot(
                    str(path),
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    getattr(metadata, "st_birthtime_ns", metadata.st_ctime_ns),
                ),
            )
        )
    with FrameworkState(database) as state:
        source_run = state.begin_initial_run(
            root,
            JournalCursor("C:", 1, 10),
            inventory_policy_signature=boundary.effective_signature,
        )
        state.publish_run_manifest(
            source_run,
            _manifest(source_run, root, selected_routes=("probe",)),
        )
        state.store_route_candidates(source_run, candidates)
        state.publish_initial_routing_snapshot(
            source_run,
            scan.scan_id,
            0,
            0,
            "incremental",
            len(candidates),
        )
        state.begin_route_runs(
            source_run,
            ("probe",),
            route_input_sources={"probe": "route_candidates"},
        )
        state.mark_abandoned_runs()

    calls = 0
    observed: list[int] = []

    def execute(context):
        nonlocal calls
        calls += 1
        rows = tuple(
            context.framework_state.iter_route_candidates(context.run_id, "application/pdf")
        )
        observed.append(len(rows))
        if calls == 1:
            raise KeyboardInterrupt
        return {"processed": len(rows)}

    route_registry = {"probe": RouteAdapter("probe", execute)}
    with pytest.raises(KeyboardInterrupt):
        FrameworkOrchestrator(
            FrameworkConfig(
                root=root,
                state_directory=state_directory,
                route="probe",
                route_only=True,
                resume_run_id=source_run,
            ),
            route_registry=route_registry,
        ).run()

    with FrameworkState(database) as state:
        failed_run = int(
            state._connection.execute("SELECT MAX(run_id) FROM initial_runs").fetchone()[0]
        )
        assert state.route_candidate_run_count(failed_run) == 24
        assert state.run_recovery_plan(failed_run)["candidates_retained"] is True

    result = FrameworkOrchestrator(
        FrameworkConfig(
            root=root,
            state_directory=state_directory,
            route="probe",
            route_only=True,
            resume_run_id=failed_run,
        ),
        route_registry=route_registry,
    ).run()
    assert result.run_id > failed_run
    assert observed == [24, 24]
    with FrameworkState(database) as state:
        # The completed run keeps its single current snapshot for read-only
        # status/replay, while older source snapshots are pruned.
        assert state.route_candidate_run_count(result.run_id) == 24
        assert state.route_candidate_run_count(failed_run) == 0
        assert state.run_recovery_plan(result.run_id)["skipped"] == ["probe"]


def test_integrated_semantic_skip_links_to_framework_run(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))

    args = Namespace(
        all=True,
        state_directory=state_directory,
        semantic_source=None,
        semantic_max_items=10,
        semantic_max_new_jobs=10,
        semantic_time_budget_seconds=1.0,
    )
    assert run_integrated_all_semantic_index(args, print_output=False, run_id=run_id) == 0

    status = list_run_status(database, run_id=run_id)[0]
    assert len(status.stages) == 1
    stage = status.stages[0]
    assert stage["stage"] == "semantic"
    assert stage["status"] == "skipped"
    details = stage["details"]
    assert isinstance(details, dict)
    publication = details["publication"]
    assert isinstance(publication, dict)
    assert publication["status"] == "absent"


def test_integrated_semantic_publication_gate_is_durable_and_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))

    args = Namespace(state_directory=state_directory)
    transaction = _begin_integrated_publication(
        args,
        run_id,
        selected_sources=("pdf", "code"),
        image_available=False,
    )
    assert transaction is not None
    assert read_state_publication_state(state_directory).status == "blocked"
    transaction.commit(transaction.prepared.owner_heads)
    view = read_state_publication_state(state_directory)
    assert view.status == "complete"
    assert view.epoch.owners == ("semantic", "code")


def test_integrated_semantic_commits_authenticated_publication_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    from tests.test_semantic_source_heads import _pdf_state

    _pdf_state(state_directory / "pdf.sqlite3")
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))

    fake_result = SimpleNamespace(
        complete=True,
        generations=(
            SimpleNamespace(
                summary=SimpleNamespace(
                    generation_id=7,
                    model_signature="fixture-model",
                    status="ready",
                    unfinished=0,
                    errors=0,
                    stale=0,
                )
            ),
        ),
    )

    def fake_index(args, *, result_sink, **_kwargs):
        from neocortex.semantic.semantic_models import EmbeddingModality
        from neocortex.semantic.semantic_schema import initialize_semantic_state
        from neocortex.semantic.semantic_state import register_embedding_model
        from tests.test_functional_defaults_publication_heads import _empty_generation, _model

        database = args.state_directory / "semantic.sqlite3"
        model = _model("fixture-model", EmbeddingModality.TEXT)
        initialize_semantic_state(database)
        register_embedding_model(database, model, allow_test_provider=True)
        generation_id = _empty_generation(
            database, model, processing_signature="fixture-published", started_ns=100
        )
        fake_result.generations[0].summary.generation_id = generation_id
        fake_result.generations[0].summary.model_signature = model.model_signature
        result_sink("text", fake_result)
        return 0

    monkeypatch.setattr(cli_semantic, "run_semantic_index", fake_index)
    args = Namespace(
        all=True,
        state_directory=state_directory,
        semantic_source=["pdf"],
        semantic_index="text",
        semantic_max_items=10,
        semantic_max_new_jobs=10,
        semantic_time_budget_seconds=30.0,
    )
    assert run_integrated_all_semantic_index(args, print_output=False, run_id=run_id) == 0
    view = read_state_publication_state(state_directory)
    assert view.status == "complete"
    assert view.epoch.owner_heads[0].owner == "semantic"
    assert view.epoch.content_manifest_name is not None


def test_integrated_semantic_truncation_keeps_progress_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    (state_directory / "pdf.sqlite3").touch()
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))

    fake_result = SimpleNamespace(
        complete=False,
        truncated=True,
        generations=(
            SimpleNamespace(
                summary=SimpleNamespace(
                    generation_id=17,
                    model_signature="fixture-model",
                    status="building",
                    unfinished=3,
                    errors=0,
                    stale=0,
                )
            ),
        ),
    )

    def fake_index(_args, *, result_sink, **_kwargs):
        result_sink("text", fake_result)
        return 0

    monkeypatch.setattr(cli_semantic, "run_semantic_index", fake_index)
    args = Namespace(
        all=True,
        state_directory=state_directory,
        semantic_source=["pdf"],
        semantic_index="text",
        semantic_max_items=10,
        semantic_max_new_jobs=10,
        semantic_time_budget_seconds=1.0,
    )
    assert run_integrated_all_semantic_index(args, print_output=False, run_id=run_id) == 2
    view = read_state_publication_state(state_directory)
    assert view.status == "absent"
    assert view.epoch.epoch == 0
    with FrameworkState(database) as state:
        stages = state.read_run_stages(run_id)
    assert stages[-1]["status"] == "partial"
    assert stages[-1]["details"]["recovery_required"] is False


def test_semantic_resume_uses_source_stage_selection_and_budget(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))
        state.publish_run_stage(
            run_id,
            "semantic",
            "interrupted",
            details={
                "selected_sources": ["pdf", "text"],
                "image_available": True,
                "semantic_budget": {
                    "max_items": 37,
                    "max_new_jobs": 113,
                    "time_budget_seconds": 42.5,
                },
                "semantic_text_profile": "compact",
                "semantic_model_cache": str(tmp_path / "models"),
                "semantic_threads": 3,
                "semantic_no_ocr": True,
            },
            idempotency_key="semantic:terminal:interrupted",
        )
    args = Namespace(
        all=False,
        state_directory=state_directory,
        semantic_source=None,
        semantic_model_cache=None,
        semantic_threads=None,
        semantic_text_profile="quality",
        semantic_no_ocr=False,
    )
    resumed = _semantic_resume_args(args, run_id)
    assert resumed is not None
    assert resumed.all is True
    assert resumed.semantic_source == ["pdf", "text"]
    assert resumed.semantic_index == "all"
    assert resumed.semantic_max_items == 37
    assert resumed.semantic_max_new_jobs == 113
    assert resumed.semantic_time_budget_seconds == 42.5
    assert resumed.semantic_text_profile == "compact"
    assert resumed.semantic_threads == 3
    assert resumed.semantic_no_ocr is True
    assert resumed.semantic_model_cache == tmp_path / "models"


def test_unbound_integrated_prepare_has_safe_temp_recovery(tmp_path: Path) -> None:
    transaction = begin_state_publication(
        tmp_path,
        operation="framework-all-semantic",
        owners=("semantic",),
        idempotency_key="fixture:semantic",
    )
    assert read_state_publication_state(tmp_path).status == "blocked"
    assert _recover_pending_integrated_publication(tmp_path) is True
    assert read_state_publication_state(tmp_path).status == "absent"
    assert transaction.prepared.owner_heads == ()


def test_integrated_publication_captures_existing_owner_heads(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))
    from neocortex.semantic.semantic_models import EmbeddingModality
    from neocortex.semantic.semantic_schema import initialize_semantic_state
    from neocortex.semantic.semantic_state import register_embedding_model
    from neocortex.semantic.semantic_publication_heads import observe_integrated_owner_heads
    from tests.test_functional_defaults_publication_heads import _empty_generation, _model

    database = state_directory / "semantic.sqlite3"
    initialize_semantic_state(database)
    model = _model("existing-owner", EmbeddingModality.TEXT)
    register_embedding_model(database, model, allow_test_provider=True)
    _empty_generation(database, model, processing_signature="fixture-baseline", started_ns=100)
    baseline = observe_integrated_owner_heads(state_directory)[0]
    record_state_publication(
        state_directory,
        operation="baseline",
        owners=("semantic",),
        status="complete",
        idempotency_key="fixture:baseline",
        owner_heads=(baseline,),
    )
    transaction = _begin_integrated_publication(
        Namespace(state_directory=state_directory),
        run_id,
        selected_sources=("pdf",),
        image_available=False,
    )
    assert transaction is not None
    assert transaction.prepared.owner_heads == (baseline,)


def test_cli_resume_routes_an_interrupted_semantic_stage_without_all_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        source_run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(source_run_id, _manifest(source_run_id, root))
        state.publish_run_stage(
            source_run_id,
            "semantic",
            "interrupted",
            details={
                "selected_sources": ["pdf"],
                "image_available": False,
                "semantic_budget": {
                    "max_items": 9,
                    "max_new_jobs": 19,
                    "time_budget_seconds": 21.0,
                },
            },
            idempotency_key="semantic:terminal:interrupted",
        )
    observed: dict[str, object] = {}

    def fake_framework(_args, *, progress):
        return SimpleNamespace(
            run_id=source_run_id + 1,
            actions=None,
            organization_plan=None,
            organization_apply=None,
            route_results={},
        )

    def fake_semantic(_args, **kwargs):
        observed.update(kwargs)
        return 0

    monkeypatch.setenv("NEOCORTEX_PROGRESS_STREAM", "1")
    monkeypatch.setattr(cli_app, "run_framework", fake_framework)
    monkeypatch.setattr(cli_semantic, "run_integrated_all_semantic_index", fake_semantic)
    monkeypatch.setattr(cli_reporting, "print_reports", lambda *_args, **_kwargs: None)
    assert (
        cli_app.main(
            [
                "--resume-run",
                str(source_run_id),
                "--root",
                str(root),
                "--state-directory",
                str(state_directory),
            ]
        )
        == 0
    )
    assert observed["resume_source_run_id"] == source_run_id
    assert observed["run_id"] == source_run_id + 1
