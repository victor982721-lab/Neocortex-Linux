"""Implementation regressions for the durable ``--all`` lifecycle.

These tests deliberately keep every corpus, Framework owner and publication
directory below ``tmp_path``.  They exercise the public orchestration and
read contracts without opening any user-owned SQLite database.
"""

from __future__ import annotations

import asyncio
import json
import time
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from neocortex.api import agent_server, public
from neocortex.api.cli import cli_app, cli_semantic
from neocortex.api.lifecycle_read_api import lifecycle_status_payload
from neocortex.api.run_lifecycle import read_run_status_json
from neocortex.capabilities.runtime import (
    CapabilityState,
    inspect_runtime_capabilities,
)
from neocortex.deduplication import (
    DedupIndex,
    FileSnapshot,
    InventoryCheckpoint,
)
from neocortex.enumeration import JournalCursor
from neocortex.persistence.framework_state_writer import (
    FrameworkState,
    RunBudgetExceeded,
)
from neocortex.persistence.state_publication import (
    StateOwnerHead,
    StatePublicationConflictError,
    read_state_publication_state,
    record_state_publication,
    require_complete_state_epoch,
)
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import (
    FrameworkOrchestrator,
    RouteExecutionError,
    build_normal_inventory_boundary,
)
from neocortex.runtime.orchestration.route_registry import (
    RouteAdapter,
    RouteLifecycleCapability,
    builtin_route_registry,
)
from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER
from neocortex.runtime.orchestration.run_manifest import RunBudget, RunManifest
from neocortex.runtime.orchestration.run_status import list_run_status
import neocortex.sdk as sdk
from neocortex.sdk import read_run_status_json as sdk_read_run_status_json


ALL_ROUTES = (
    "pdf",
    "docx",
    "office",
    "archive",
    "text",
    "audio",
    "video",
    "image",
    "code",
)

# The MCP parity regression is skipped by the repository capability gate when
# the optional agent runtime is not installed.
TEST_CAPABILITIES = ("agent",)


@dataclass(frozen=True, slots=True)
class _SourceFixture:
    root: Path
    state_directory: Path
    database: Path
    run_id: int
    scan_id: int
    candidate_count: int
    candidate_bytes: int


def _root_identity(root: Path) -> tuple[int, int, int]:
    metadata = root.stat()
    return (int(metadata.st_dev), int(metadata.st_ino), stat_birthtime_ns(metadata))


def _make_source_fixture(
    tmp_path: Path,
    *,
    candidate_count: int = 1,
    route_names: tuple[str, ...] = (),
    completed_routes: tuple[str, ...] = (),
    route_capabilities: dict[str, str] | None = None,
) -> _SourceFixture:
    """Create one complete or interrupted source run with a real scan binding."""

    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    paths = []
    for index in range(candidate_count):
        path = root / f"fixture-{index:03d}.pdf"
        path.write_bytes(b"%PDF-1.4\nfixture-" + str(index).encode("ascii"))
        paths.append(path)

    dedup_database = state_directory / "dedup.sqlite3"
    boundary = build_normal_inventory_boundary(root, state_directory)
    with DedupIndex(dedup_database) as index:
        scan = index.scan(root, exclusion_policy=boundary.exclusion_policy)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(
                str(boundary.access_policy.root),
                scan.scan_id,
                "C:",
                1,
                11,
                True,
                boundary.exclusion_policy.signature,
            )
        )

    snapshots = tuple(
        (
            "application/pdf",
            FileSnapshot(
                str(path),
                int(path.stat().st_dev),
                int(path.stat().st_ino),
                int(path.stat().st_size),
                int(path.stat().st_mtime_ns),
                stat_birthtime_ns(path.stat()),
            ),
        )
        for path in paths
    )
    selected_routes = tuple(route_names) if route_names else ("text",)
    capabilities = route_capabilities or dict.fromkeys(selected_routes, "safe_replay")
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(
            root,
            JournalCursor("C:", 1, 10),
            inventory_policy_signature=boundary.effective_signature,
        )
        state.store_route_candidates(run_id, snapshots)
        state.publish_initial_routing_snapshot(
            run_id,
            scan.scan_id,
            0,
            1,
            "full",
            len(snapshots),
        )
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(root),
                root_identity=_root_identity(root),
                selected_routes=selected_routes,
                route_capabilities=capabilities,
                configuration={"fixture": True},
                budget={"durable": RunBudget().payload()},
                input_snapshot={
                    "scan_id": scan.scan_id,
                    "candidate_rows": len(snapshots),
                },
            ).event_payload(),
        )
        if route_names:
            state.begin_route_runs(
                run_id,
                route_names,
                route_input_sources=dict.fromkeys(route_names, "route_candidates"),
            )
            for route in completed_routes:
                state.complete_route_run(
                    run_id,
                    route,
                    {"candidates": len(snapshots), "processed": len(snapshots)},
                )
            state.mark_abandoned_runs()
        else:
            state.complete_initial_run(
                run_id,
                scan.scan_id,
                JournalCursor("C:", 1, 11),
                0,
                1,
                "full",
            )

    return _SourceFixture(
        root=root,
        state_directory=state_directory,
        database=database,
        run_id=run_id,
        scan_id=scan.scan_id,
        candidate_count=len(snapshots),
        candidate_bytes=sum(snapshot.size for _, snapshot in snapshots),
    )


def _route_registry(
    source: _SourceFixture,
    calls: list[tuple[str, int]],
    *,
    route_names: tuple[str, ...],
    execute_override=None,
    capabilities: dict[str, str] | None = None,
) -> dict[str, RouteAdapter]:
    registry: dict[str, RouteAdapter] = {}
    for name in route_names:
        def execute(context, *, _name=name):
            calls.append((_name, context.run_id))
            if execute_override is not None:
                return execute_override(_name, context)
            return {
                "candidates": source.candidate_count,
                "processed": source.candidate_count,
                "new_work": source.candidate_count,
            }

        registry[name] = RouteAdapter(
            name,
            execute,
            lifecycle_capability=cast(
                RouteLifecycleCapability,
                (capabilities or {}).get(name, "safe_replay"),
            ),
        )
    return registry


def _run_route_only(
    source: _SourceFixture,
    registry: dict[str, RouteAdapter],
    *,
    route: str,
    resume: bool = False,
    run_budget: RunBudget | dict[str, object] | None = None,
    source_run_id: int | None = None,
):
    selected_source = source.run_id if source_run_id is None else source_run_id
    return FrameworkOrchestrator(
        FrameworkConfig(
            root=source.root,
            state_directory=source.state_directory,
            route=route,
            route_only=True,
            candidate_run_id=None if resume else selected_source,
            resume_run_id=selected_source if resume else None,
            heartbeat_interval_seconds=0.01,
            global_resource_wait_timeout_seconds=5.0,
        ),
        route_registry=registry,
        run_budget=run_budget,
    ).run()


def test_all_contract_closes_nine_routes_and_types_missing_dependencies() -> None:
    registry = builtin_route_registry()
    assert tuple(registry) == ALL_ROUTES == BUILTIN_ROUTE_ORDER
    assert registry["pdf"].lifecycle_capability == "phase_resume"
    assert all(
        registry[name].lifecycle_capability == "safe_replay"
        for name in ALL_ROUTES
        if name != "pdf"
    )

    statuses = inspect_runtime_capabilities(
        ALL_ROUTES,
        module_finder=lambda _module: None,
        distribution_version=lambda _distribution: "missing",
        executable_finder=lambda _executable: None,
    )
    assert tuple(status.capability for status in statuses) == ALL_ROUTES
    audio = next(status for status in statuses if status.capability == "audio")
    assert audio.state is CapabilityState.UNAVAILABLE
    assert "audio_backend_unavailable" in audio.degradation_reasons
    assert audio.to_dict()["operational_state"] == "blocked_by_requirements"


def test_all_runs_twenty_fixtures_and_publishes_route_capabilities(
    tmp_path: Path,
) -> None:
    source = _make_source_fixture(tmp_path, candidate_count=20)
    calls: list[tuple[str, int]] = []
    registry = _route_registry(
        source,
        calls,
        route_names=ALL_ROUTES,
        capabilities={"pdf": "phase_resume"},
    )

    result = _run_route_only(source, registry, route="all")

    assert result.run_id > source.run_id
    assert {name for name, _run_id in calls} == set(ALL_ROUTES)
    assert len(calls) == len(ALL_ROUTES)
    statuses = list_run_status(source.database, run_id=result.run_id, limit=1)
    assert len(statuses) == 1
    status = statuses[0]
    assert status.status == "completed"
    assert status.manifest is not None
    manifest = cast(dict[str, Any], status.manifest)
    assert set(manifest["selected_routes"]) == set(ALL_ROUTES)
    assert manifest["route_capabilities"]["pdf"] == "phase_resume"
    assert set(manifest["route_capabilities"]) == set(ALL_ROUTES)
    assert {route.route_name for route in status.routes} == set(ALL_ROUTES)
    assert all(route.status == "completed" for route in status.routes)
    assert all(route.candidates == 20 for route in status.routes)
    assert all(
        route.resume_capability == manifest["route_capabilities"][route.route_name]
        for route in status.routes
    )
    assert status.budget is not None
    assert status.budget["consumed_items"] == 20 * len(ALL_ROUTES)


def test_resume_skips_completed_route_and_replays_only_pending_route(
    tmp_path: Path,
) -> None:
    source = _make_source_fixture(
        tmp_path,
        route_names=("text", "pdf"),
        completed_routes=("text",),
        route_capabilities={"text": "safe_replay", "pdf": "phase_resume"},
    )
    calls: list[tuple[str, int]] = []
    registry = _route_registry(source, calls, route_names=("text", "pdf"))

    result = _run_route_only(source, registry, route="none", resume=True)

    assert [name for name, _run_id in calls] == ["pdf"]
    resumed = list_run_status(source.database, run_id=result.run_id, limit=1)[0]
    assert [route.route_name for route in resumed.routes] == ["pdf"]
    with FrameworkState(source.database) as state:
        recovery = state.run_recovery_plan(source.run_id)
    assert recovery["skipped"] == ["text"]
    assert recovery["pending"] == ["pdf"]


def test_replay_reports_zero_new_work_without_duplicate_effects(tmp_path: Path) -> None:
    source = _make_source_fixture(tmp_path, candidate_count=20)
    calls: list[tuple[str, int]] = []
    invocation = 0

    def execute(_name: str, _context):
        nonlocal invocation
        invocation += 1
        if invocation == 1:
            return {"candidates": 20, "processed": 20, "new_work": 20}
        return {
            "candidates": 20,
            "processed": 20,
            "cache_hits": 20,
            "new_work": 0,
        }

    registry = _route_registry(
        source,
        calls,
        route_names=("text",),
        execute_override=execute,
    )
    first = _run_route_only(source, registry, route="text")
    second = _run_route_only(
        source,
        registry,
        route="text",
        source_run_id=first.run_id,
    )

    assert first.run_id != second.run_id
    assert len(calls) == 2
    status = list_run_status(source.database, run_id=second.run_id, limit=1)[0]
    route = status.routes[0]
    assert route.new_work == 0
    assert route.cache_hits == 20
    assert route.replay_status == "replayed"
    assert status.skipped_routes == ()


def test_unavailable_route_keeps_independent_route_result_and_cause(
    tmp_path: Path,
) -> None:
    source = _make_source_fixture(tmp_path)
    calls: list[tuple[str, int]] = []

    def execute(name: str, _context):
        if name == "audio":
            raise ModuleNotFoundError("No module named 'faster_whisper'")
        return {"candidates": 1, "processed": 1, "new_work": 1}

    registry = _route_registry(
        source,
        calls,
        route_names=("text", "audio"),
        execute_override=execute,
    )
    with pytest.raises(RouteExecutionError, match="faster_whisper"):
        _run_route_only(source, registry, route="text,audio")

    status = list_run_status(source.database, limit=1)[0]
    routes = {route.route_name: route for route in status.routes}
    assert routes["text"].status == "completed"
    assert routes["audio"].status == "failed"
    assert routes["audio"].error_type == "ModuleNotFoundError"
    assert "audio" not in status.skipped_routes
    assert status.status == "failed"


def test_global_budget_covers_route_semantic_and_final_deadline_gate(
    tmp_path: Path,
) -> None:
    source = _make_source_fixture(tmp_path)
    with FrameworkState(source.database) as state:
        run_id = state.begin_operational_run(
            source.root,
            run_kind="route_only",
            source_run_id=source.run_id,
        )
        budget = RunBudget(
            max_items=2,
            max_bytes=source.candidate_bytes * 2,
            max_duration_seconds=0.001,
        )
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="route_only",
                source_run_id=source.run_id,
                root=str(source.root),
                root_identity=_root_identity(source.root),
                selected_routes=("text",),
                budget={"durable": budget.payload()},
            ).event_payload(),
        )
        state.reserve_run_budget(
            run_id,
            "inventory",
            items=1,
            bytes=source.candidate_bytes,
        )
        state.reserve_run_budget(
            run_id,
            "route:text",
            items=1,
            bytes=source.candidate_bytes,
        )
        with pytest.raises(RunBudgetExceeded, match="items"):
            state.reserve_run_budget(run_id, "semantic", items=1)
        snapshot = state.read_run_budget(run_id)
        assert snapshot is not None
        assert snapshot["consumed_items"] == 2
        assert snapshot["remaining_items"] == 0
        time.sleep(0.01)
        with pytest.raises(RunBudgetExceeded, match="time"):
            state.complete_operational_run(run_id)
        assert state.cancel_initial_run(run_id)


def test_keyboard_interrupt_is_durable_and_second_resume_is_idempotent(
    tmp_path: Path,
) -> None:
    source = _make_source_fixture(
        tmp_path,
        route_names=("probe",),
        route_capabilities={"probe": "safe_replay"},
    )
    calls: list[tuple[str, int]] = []
    first_attempt = True

    def interrupt_once(_name: str, _context):
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            raise KeyboardInterrupt("fixture interruption")
        return {"candidates": 1, "processed": 1, "new_work": 1}

    registry = _route_registry(
        source,
        calls,
        route_names=("probe",),
        execute_override=interrupt_once,
    )
    with pytest.raises(KeyboardInterrupt, match="fixture interruption"):
        _run_route_only(source, registry, route="none", resume=True)
    first_status = list_run_status(source.database, limit=1)[0]
    assert first_status.status == "cancelled"
    assert first_status.routes[0].status == "cancelled"

    result = _run_route_only(source, registry, route="none", resume=True)
    second_status = list_run_status(source.database, run_id=result.run_id, limit=1)[0]
    assert second_status.status == "completed"
    assert second_status.replayed is True
    assert len(calls) == 2


def test_not_resumable_route_is_rejected_before_creating_a_new_run(
    tmp_path: Path,
) -> None:
    source = _make_source_fixture(
        tmp_path,
        route_names=("probe",),
        route_capabilities={"probe": "not_resumable"},
    )
    calls: list[tuple[str, int]] = []
    registry = _route_registry(
        source,
        calls,
        route_names=("probe",),
        capabilities={"probe": "not_resumable"},
    )
    with pytest.raises(ValueError, match="non-replayable"):
        _run_route_only(source, registry, route="none", resume=True)
    assert calls == []
    with FrameworkState(source.database) as state:
        assert state._connection.execute("SELECT COUNT(*) FROM initial_runs").fetchone()[0] == 1


def test_resume_rejects_a_replaced_root_before_new_run_or_worker(
    tmp_path: Path,
) -> None:
    source = _make_source_fixture(tmp_path, route_names=("text",))
    old_root = source.root.with_name("corpus-old")
    source.root.rename(old_root)
    source.root.mkdir()
    calls: list[tuple[str, int]] = []
    registry = _route_registry(source, calls, route_names=("text",))

    with pytest.raises(
        (ValueError, PermissionError),
        match=r"identity changed|replaced corpus root",
    ):
        _run_route_only(source, registry, route="none", resume=True)
    assert calls == []
    with FrameworkState(source.database) as state:
        assert state._connection.execute("SELECT COUNT(*) FROM initial_runs").fetchone()[0] == 1


def test_owner_head_drift_blocks_cross_owner_epoch_without_partial_success(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "publication"
    state_directory.mkdir()
    baseline = (
        StateOwnerHead("code", 1, "b" * 64),
        StateOwnerHead("semantic", 1, "a" * 64),
    )
    record_state_publication(
        state_directory,
        operation="fixture-baseline",
        owners=("semantic", "code"),
        status="complete",
        idempotency_key="fixture-baseline",
        owner_heads=baseline,
    )
    drifted = (
        StateOwnerHead("code", 1, "b" * 64),
        StateOwnerHead("semantic", 2, "c" * 64),
    )
    with pytest.raises(StatePublicationConflictError, match="owner heads"):
        require_complete_state_epoch(state_directory, owner_heads=drifted)
    view = read_state_publication_state(state_directory)
    assert view.status == "complete"
    assert view.epoch.epoch == 1
    assert view.epoch.owner_heads == baseline


def test_semantic_only_resume_reuses_source_selection_and_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source_fixture(tmp_path)
    with FrameworkState(source.database) as state:
        state.publish_run_stage(
            source.run_id,
            "semantic",
            "interrupted",
            details={
                "selected_sources": ["text"],
                "image_available": False,
                "semantic_budget": {
                    "max_items": 37,
                    "max_new_jobs": 113,
                    "time_budget_seconds": 42.5,
                },
                "semantic_text_profile": "compact",
                "semantic_threads": 3,
                "semantic_no_ocr": True,
            },
            idempotency_key="semantic:fixture:interrupted",
        )

    observed: dict[str, object] = {}
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

    def fake_index(args, *, result_sink, **kwargs):
        observed.update(
            {
                "semantic_source": tuple(args.semantic_source),
                "semantic_max_items": args.semantic_max_items,
                "semantic_max_new_jobs": args.semantic_max_new_jobs,
                "semantic_time_budget_seconds": args.semantic_time_budget_seconds,
                "kwargs": kwargs,
            }
        )
        result_sink("text", fake_result)
        return 0

    monkeypatch.setattr(cli_semantic, "run_semantic_index", fake_index)
    args = Namespace(
        all=False,
        state_directory=source.state_directory,
        semantic_source=None,
        semantic_model_cache=None,
        semantic_threads=None,
        semantic_text_profile="quality",
        semantic_no_ocr=False,
    )
    assert (
        cli_semantic.run_integrated_all_semantic_index(
            args,
            print_output=False,
            run_id=source.run_id,
            resume_source_run_id=source.run_id,
        )
        == 0
    )
    assert observed["semantic_source"] == ("text",)
    assert observed["semantic_max_items"] == 37
    assert observed["semantic_max_new_jobs"] == 113
    assert observed["semantic_time_budget_seconds"] == 42.5
    with FrameworkState(source.database) as state:
        stages = state.read_run_stages(source.run_id)
    assert stages[-1]["stage"] == "semantic"
    assert stages[-1]["status"] == "completed"


def test_completed_framework_with_partial_stage_is_not_reported_as_complete(
    tmp_path: Path,
) -> None:
    source = _make_source_fixture(tmp_path)
    with FrameworkState(source.database) as state:
        state.publish_run_stage(
            source.run_id,
            "semantic",
            "partial",
            details={"recovery_required": False},
            idempotency_key="semantic:partial",
        )

    status = list_run_status(source.database, run_id=source.run_id, limit=1)[0]
    assert status.status == "partial"
    assert status.current_phase == "semantic"


def test_integrated_stage_runner_sees_pending_stage_before_framework_finalize(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    observed: dict[str, object] = {}

    def run_stage(run_id: int) -> object:
        with FrameworkState(state_directory / "framework.sqlite3") as state:
            row = state._connection.execute(
                "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            observed["run_status"] = None if row is None else str(row[0])
            stages = state.read_run_stages(run_id)
            observed["pending"] = stages[-1]["status"]
            state.publish_run_stage(
                run_id,
                "semantic",
                "completed",
                details={"publication": {"status": "complete"}},
                idempotency_key="semantic:fixture:completed",
            )
        return 0

    result = FrameworkOrchestrator(
        FrameworkConfig(
            root=root,
            state_directory=state_directory,
            route="none",
            global_min_free_memory_bytes=0,
            global_min_free_commit_bytes=0,
        ),
        lifecycle_stage_runner=run_stage,
        lifecycle_stage_details={"selection_pending": True},
    ).run()

    assert observed == {"run_status": "running", "pending": "pending"}
    status = list_run_status(state_directory / "framework.sqlite3", run_id=result.run_id)[0]
    assert status.status == "completed"
    assert status.stages[-1]["status"] == "completed"


def test_cli_runs_semantic_as_a_framework_lifecycle_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neocortex.api.cli import cli_reporting

    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    observed: dict[str, object] = {}

    def fake_semantic(_args, **kwargs):
        observed["semantic_kwargs"] = kwargs
        return 0

    def fake_framework(
        _args,
        *,
        progress,
        lifecycle_stage_runner,
        lifecycle_stage_details,
    ):
        observed["stage_details"] = lifecycle_stage_details
        lifecycle_stage_runner(7)
        return SimpleNamespace(
            run_id=7,
            actions=None,
            organization_plan=None,
            organization_apply=None,
            route_results={},
        )

    monkeypatch.setenv("NEOCORTEX_PROGRESS_STREAM", "1")
    monkeypatch.setattr(cli_app, "run_framework", fake_framework)
    monkeypatch.setattr(cli_semantic, "run_integrated_all_semantic_index", fake_semantic)
    monkeypatch.setattr(cli_reporting, "print_reports", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_reporting,
        "print_professional_summary",
        lambda *_args, **_kwargs: None,
    )

    assert (
        cli_app.main(
            [
                "--all",
                "--root",
                str(root),
                "--state-directory",
                str(state_directory),
            ]
        )
        == 0
    )
    semantic_kwargs = cast(dict[str, Any], observed["semantic_kwargs"])
    stage_details = cast(dict[str, Any], observed["stage_details"])
    assert semantic_kwargs["run_id"] == 7
    assert semantic_kwargs["resume_source_run_id"] is None
    assert stage_details["selection_pending"] is True


@pytest.mark.capability("agent")
def test_cli_api_sdk_and_mcp_expose_the_same_read_only_lifecycle_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _make_source_fixture(tmp_path)
    calls: list[tuple[str, int]] = []
    registry = _route_registry(source, calls, route_names=("text",))
    result = _run_route_only(source, registry, route="text")
    database_before = source.database.read_bytes()

    canonical_json = read_run_status_json(
        source.database,
        limit=1,
        run_id=result.run_id,
    )[0]
    api_json = public.read_run_status_json(
        source.database,
        limit=1,
        run_id=result.run_id,
    )[0]
    sdk_json = sdk_read_run_status_json(
        source.database,
        limit=1,
        run_id=result.run_id,
    )[0]
    assert json.loads(api_json) == json.loads(canonical_json)
    assert json.loads(sdk_json) == json.loads(canonical_json)

    payload = lifecycle_status_payload(
        limit=1,
        run_id=result.run_id,
        state_directory=source.state_directory,
    )
    assert payload["read_only"] is True
    payload_map = cast(dict[str, Any], payload)
    run_payload = cast(dict[str, Any], payload_map["runs"][0])
    assert run_payload["run_id"] == result.run_id
    assert run_payload["lifecycle"]["schema"] == "neocortex.lifecycle-envelope/v1"
    api_payload = public.lifecycle_status_payload(
        limit=1,
        run_id=result.run_id,
        state_directory=source.state_directory,
    )
    sdk_payload = sdk.lifecycle_status_payload(
        limit=1,
        run_id=result.run_id,
        state_directory=source.state_directory,
    )

    def without_request_id(value):
        selected = dict(value)
        selected.pop("request_id", None)
        return selected

    assert without_request_id(api_payload) == without_request_id(payload)
    assert without_request_id(sdk_payload) == without_request_id(payload)

    assert (
        cli_app.main(
            [
                "--status",
                "--status-json",
                "--status-run",
                str(result.run_id),
                "--status-limit",
                "1",
                "--state-directory",
                str(source.state_directory),
            ]
        )
        == 0
    )
    cli_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(cli_lines) == 1
    assert without_request_id(json.loads(cli_lines[0])) == without_request_id(payload)

    monkeypatch.setattr(
        agent_server,
        "lifecycle_status_payload",
        lambda *, limit=5, run_id=None: lifecycle_status_payload(
            limit=limit,
            run_id=run_id,
            state_directory=source.state_directory,
        ),
    )
    server = agent_server.create_server()
    _content, mcp_payload = asyncio.run(
        server.call_tool(
            "lifecycle_status",
            {"limit": 1, "run_id": result.run_id},
        )
    )
    assert mcp_payload["read_only"] is True
    assert without_request_id(mcp_payload) == without_request_id(payload)
    assert source.database.read_bytes() == database_before
