from __future__ import annotations

import gc
import inspect
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tests.portable_inventory import PortableInventoryCursor
from neocortex.deduplication import (
    DedupIndex,
    FileSnapshot,
    InventoryCheckpoint,
    ScanSummary,
)
from neocortex.runtime.models import FrameworkConfig, RouteOnlyRunResult
from neocortex.integrations.inventory.inventory_boundary import NormalInventoryBoundary
from neocortex.runtime.orchestration import orchestrator as orchestrator_module
from neocortex.runtime.control.global_resources import GlobalResourceSummary
from neocortex.runtime.orchestration.orchestrator import (
    FrameworkOrchestrator,
    build_normal_inventory_boundary,
)
from neocortex.safety.route_filters import CandidateSelection
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.runtime.orchestration.run_status import list_run_status
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.orchestration.run_manifest import RunManifest


# region [01] Route-only and resumable execution


def _bind_policy_checkpoint(
    index: DedupIndex,
    database: Path,
    root: Path,
) -> tuple[ScanSummary, str]:
    boundary = build_normal_inventory_boundary(
        root,
        database.parent,
        observe_regenerable_artifacts=True,
    )
    scan = index.scan(root, exclusion_policy=boundary.exclusion_policy)
    index.bind_inventory_checkpoint(
        InventoryCheckpoint(
            str(boundary.access_policy.root),
            scan.scan_id,
            True,
            boundary.exclusion_policy.signature,
        )
    )
    return scan, boundary.effective_signature




def _source_run(
    database: Path,
    root: Path,
    *,
    route_running: bool = False,
    persist_policy: bool = True,
    manifest_budget: Mapping[str, object] | None = None,
) -> int:
    source_path = root / "one.pdf"
    source_path.write_bytes(b"%PDF-1.4\n")
    with DedupIndex(database.with_name("dedup.sqlite3")) as index:
        scan, effective_signature = _bind_policy_checkpoint(
            index,
            database,
            root,
        )
    source_stat = source_path.stat()
    snapshot = FileSnapshot(
        str(source_path),
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        getattr(source_stat, "st_birthtime_ns", source_stat.st_ctime_ns),
    )
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(
            root,
            PortableInventoryCursor("C:", 1, 10),
            inventory_policy_signature=(effective_signature if persist_policy else None),
        )
        if persist_policy:
            state.publish_run_manifest(
                run_id,
                RunManifest(
                    run_id=run_id,
                    run_kind="initial",
                    root=str(root),
                    root_identity=(1, 2, -1),
                    selected_routes=("probe",),
                    route_capabilities={"probe": "safe_replay"},
            configuration={},
                    budget={} if manifest_budget is None else dict(manifest_budget),
                ).event_payload(),
            )
        state.store_route_candidates(run_id, (("application/pdf", snapshot),))
        state.publish_initial_routing_snapshot(
            run_id,
            scan.scan_id,
            0,
            0,
            "incremental",
            1,
        )
        if route_running:
            state.begin_route_runs(run_id, ("probe",))
            state.begin_route_phase(run_id, "probe", "extraction")
            state.complete_route_phase(run_id, "probe", "extraction", {"rows": 1})
        else:
            state.complete_initial_run(
                run_id,
                scan.scan_id,
                PortableInventoryCursor("C:", 1, 11),
                0,
                0,
                "incremental",
            )
    return run_id






class _RouteOnlyBoundaryDouble:
    def __init__(self, root: Path, events: list[str]) -> None:
        self.access_policy = SimpleNamespace(root=root)
        self.exclusion_policy = SimpleNamespace(signature="exclusion-signature")
        self.effective_signature = "effective-signature"
        self._events = events

    def verify(self) -> None:
        self._events.append("boundary.verify")


class _RouteOnlyStateDouble:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.recorded_details: dict[str, dict[str, object]] = {}
        self._run_manifest = {"configuration": {}}

    def read_run_manifest(self, _run_id: int) -> dict[str, object]:
        return self._run_manifest

    def __enter__(self):
        self.events.append("state.enter")
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        suffix = "none" if exc_type is None else exc_type.__name__
        self.events.append(f"state.exit:{suffix}")

    def route_candidate_run_count(self, run_id: int) -> int:
        self.events.append(f"state.route_candidate_run_count:{run_id}")
        return 3

    def begin_operational_run(
        self,
        root: Path,
        *,
        run_kind: str,
        source_run_id: int,
    ) -> int:
        self.events.append(f"state.begin:{root.name}:{run_kind}:{source_run_id}")
        return 84

    def copy_route_candidates(self, source_run_id: int, run_id: int) -> int:
        self.events.append(f"state.copy:{source_run_id}:{run_id}")
        return 3

    def record_event(
        self,
        run_id: int,
        severity: str,
        phase: str,
        message: str,
        details: dict[str, object],
    ) -> None:
        del run_id, phase
        if message.endswith("iniciada"):
            label = "start"
        elif message.endswith("completada"):
            label = "complete"
        else:
            label = "failure"
        self.events.append(f"state.event:{severity}:{label}")
        self.recorded_details[label] = details

    def set_run_phase(self, run_id: int, phase: str) -> None:
        self.events.append(f"state.phase:{run_id}:{phase}")

    def prune_route_candidates(self, run_ids: tuple[int, ...]) -> None:
        self.events.append(f"state.prune:{run_ids[0]}")

    def complete_operational_run(self, run_id: int) -> None:
        self.events.append(f"state.complete:{run_id}")

    def cancel_initial_run(self, run_id: int) -> None:
        self.events.append(f"state.cancel:{run_id}")

    def fail_initial_run(self, run_id: int) -> None:
        self.events.append(f"state.fail:{run_id}")


def _route_only_doubles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_action: Callable[[], tuple[dict[str, object], object | None]],
) -> tuple[
    FrameworkOrchestrator,
    NormalInventoryBoundary,
    _RouteOnlyStateDouble,
    list[str],
]:
    events: list[str] = []
    state = _RouteOnlyStateDouble(events)
    boundary = _RouteOnlyBoundaryDouble(tmp_path, events)
    framework = FrameworkOrchestrator(
        FrameworkConfig(
            root=tmp_path,
            state_directory=tmp_path / "state",
            route="probe",
            route_only=True,
            candidate_run_id=41,
            heartbeat_interval_seconds=0.25,
        ),
        route_registry={"probe": RouteAdapter("probe", lambda _context: None)},
    )

    def reusable_source_scan_id(
        selected_state,
        source_run_id: int,
        selected_boundary,
        expected_scan_id: int | None,
    ) -> int:
        assert selected_state is state
        assert selected_boundary is boundary
        assert source_run_id == 41
        assert expected_scan_id is None
        events.append("source.scan")
        return 73

    def run_content_routes(*, root, state, run_id, scan_id):
        assert root == tmp_path
        assert state is state_double
        assert run_id == 84
        assert scan_id == 73
        events.append("routes.execute")
        return route_action()

    state_double = state

    class _HeartbeatDouble:
        def __init__(
            self,
            database: Path,
            run_id: int,
            *,
            interval_seconds: float,
        ) -> None:
            assert database == framework.config.framework_database
            assert run_id == 84
            assert interval_seconds == 0.25
            events.append("heartbeat.init")

        def start(self):
            events.append("heartbeat.start")
            return self

        def stop(self) -> None:
            events.append("heartbeat.stop")

    monkeypatch.setattr(orchestrator_module, "FrameworkState", lambda _path: state)
    monkeypatch.setattr(orchestrator_module, "RunHeartbeat", _HeartbeatDouble)
    monkeypatch.setattr(
        framework,
        "_reusable_source_scan_id",
        reusable_source_scan_id,
    )
    monkeypatch.setattr(framework, "_run_content_routes", run_content_routes)
    return framework, cast(NormalInventoryBoundary, boundary), state, events


def test_locked_route_only_signature_phase_order_and_complete_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert str(inspect.signature(FrameworkOrchestrator._run_route_only_locked)) == (
        "(self, boundary: 'NormalInventoryBoundary') -> 'RouteOnlyRunResult'"
    )
    summaries: dict[str, object] = {
        name: SimpleNamespace(name=name)
        for name in ("pdf", "docx", "office", "audio", "image")
    }
    summaries["probe"] = SimpleNamespace(name="probe")
    resources = cast(GlobalResourceSummary, SimpleNamespace(name="resources"))
    framework, boundary, state, events = _route_only_doubles(
        tmp_path,
        monkeypatch,
        lambda: (summaries, resources),
    )

    result = framework._run_route_only_locked(boundary)

    assert result.run_id == 84
    assert result.source_run_id == 41
    assert result.route_results is summaries
    assert result.global_resources is resources
    assert result.pdf is summaries["pdf"]
    assert result.docx is summaries["docx"]
    assert result.office is summaries["office"]
    assert result.audio is summaries["audio"]
    assert result.image is summaries["image"]
    assert result.actions.apply_actions is False
    assert events == [
        "boundary.verify",
        "state.enter",
        "state.route_candidate_run_count:41",
        "source.scan",
        "boundary.verify",
        f"state.begin:{tmp_path.name}:route_only:41",
        "state.copy:41:84",
        "heartbeat.init",
        "heartbeat.start",
        "state.event:info:start",
        "routes.execute",
        "boundary.verify",
        "state.phase:84:finalize",
        "state.prune:84",
        "state.complete:84",
        "state.event:info:complete",
        "heartbeat.stop",
        "state.exit:none",
    ]
    start = state.recorded_details["start"]
    assert start["source_run_id"] == 41
    assert start["candidate_rows"] == 3
    assert start["source_candidate_rows"] == 3
    assert start["route_input_sources"] == {"probe": "route_candidates"}
    assert start["selected_routes"] == ["probe"]
    assert start["resume"] is False
    assert state.recorded_details["complete"] == {"source_run_id": 41}


def test_locked_route_only_propagates_cancellation_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = KeyboardInterrupt("cancel route-only")

    def cancel() -> tuple[dict[str, object], object | None]:
        raise cancellation

    framework, boundary, _state, events = _route_only_doubles(
        tmp_path,
        monkeypatch,
        cancel,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        framework._run_route_only_locked(boundary)
    assert raised.value is cancellation
    assert events[-5:] == [
        "state.event:info:start",
        "routes.execute",
        "state.cancel:84",
        "heartbeat.stop",
        "state.exit:KeyboardInterrupt",
    ]
    assert not any(event.startswith("state.event:error") for event in events)


def test_locked_route_only_records_failure_before_marking_run_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError("route-only failure")

    def fail() -> tuple[dict[str, object], object | None]:
        raise failure

    framework, boundary, state, events = _route_only_doubles(
        tmp_path,
        monkeypatch,
        fail,
    )

    with pytest.raises(RuntimeError) as raised:
        framework._run_route_only_locked(boundary)
    assert raised.value is failure
    assert events[-5:] == [
        "routes.execute",
        "state.event:error:failure",
        "state.fail:84",
        "heartbeat.stop",
        "state.exit:RuntimeError",
    ]
    assert state.recorded_details["failure"] == {
        "error_type": "RuntimeError",
        "detail": "route-only failure",
    }


def test_route_only_reuses_retained_candidates_without_inventory(tmp_path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    source = _source_run(state_dir / "framework.sqlite3", tmp_path)
    seen: list[str] = []

    def execute(context):
        seen.extend(
            snapshot.path
            for snapshot in context.framework_state.iter_route_candidates(
                context.run_id, "application/pdf"
            )
        )
        return {"processed": len(seen)}

    config = FrameworkConfig(
        root=tmp_path,
        state_directory=state_dir,
        route="probe",
        route_only=True,
        candidate_run_id=source,
        heartbeat_interval_seconds=0.01,
    )
    result = FrameworkOrchestrator(
        config,
        route_registry={"probe": RouteAdapter("probe", execute)},
    ).run()

    assert isinstance(result, RouteOnlyRunResult)
    assert result.source_run_id == source
    assert result.route_results["probe"] == {"processed": 1}
    assert seen == [str(tmp_path / "one.pdf")]
    with closing(sqlite3.connect(state_dir / "framework.sqlite3")) as connection, connection:
        latest = connection.execute(
            """SELECT run_kind,status,source_run_id,current_phase
            FROM initial_runs ORDER BY run_id DESC LIMIT 1"""
        ).fetchone()
        actions = connection.execute(
            "SELECT COUNT(*) FROM file_actions WHERE run_id=?",
            (result.run_id,),
        ).fetchone()[0]
    assert latest == ("route_only", "completed", source, "completed")
    assert actions == 0












def test_resume_infers_interrupted_route_and_preserves_phase_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    source = _source_run(
        state_dir / "framework.sqlite3",
        tmp_path,
        route_running=True,
    )

    def execute(context):
        from neocortex.persistence import sqlite_immutable

        original_copy = sqlite_immutable._copy_regular_file

        def copy_during_progress(source_path, destination):
            original_copy(source_path, destination)
            if source_path == state_dir / "framework.sqlite3":
                context.framework_state.record_event(
                    context.run_id, "info", "probe", "concurrent progress"
                )
                gc.collect()

        with monkeypatch.context() as patch:
            patch.setattr(sqlite_immutable, "_copy_regular_file", copy_during_progress)
            completed = context.framework_state.completed_route_phases(source, "probe")
        return {"source_extraction_complete": "extraction" in completed}

    config = FrameworkConfig(
        root=tmp_path,
        state_directory=state_dir,
        route="none",
        route_only=True,
        resume_run_id=source,
        heartbeat_interval_seconds=0.01,
    )
    result = FrameworkOrchestrator(
        config,
        route_registry={"probe": RouteAdapter("probe", execute)},
    ).run()

    assert isinstance(result, RouteOnlyRunResult)
    probe_result = result.route_results["probe"]
    assert isinstance(probe_result, dict)
    assert probe_result["source_extraction_complete"] is True
    with closing(sqlite3.connect(state_dir / "framework.sqlite3")) as connection, connection:
        source_status = connection.execute(
            "SELECT status FROM initial_runs WHERE run_id=?",
            (source,),
        ).fetchone()[0]
    assert source_status == "interrupted"






# endregion [01]


# region [02] Atomic routing snapshot publication


def test_routing_snapshot_accepts_zero_incremental_attempts(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(
            tmp_path,
            PortableInventoryCursor("C:", 1, 10),
        )

        assert state.publish_initial_routing_snapshot(
            run_id,
            7,
            0,
            0,
            "incremental",
            0,
        )
        state.complete_initial_run(
            run_id,
            7,
            PortableInventoryCursor("C:", 1, 11),
            0,
            0,
            "incremental",
        )

    with closing(sqlite3.connect(database)) as connection, connection:
        row = connection.execute(
            """SELECT status,scan_id,reconciliation_records,inventory_attempts,
            inventory_mode FROM initial_runs WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        details = json.loads(
            connection.execute(
                """SELECT details_json FROM run_events WHERE run_id=?
                AND phase='routing-snapshot'""",
                (run_id,),
            ).fetchone()[0]
        )
    assert row == ("completed", 7, 0, 0, "incremental")
    assert details["attempts"] == 0
    assert details["candidate_rows"] == 0


def test_route_runs_cannot_start_before_snapshot_publication(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(
            tmp_path,
            PortableInventoryCursor("C:", 1, 10),
        )

        with pytest.raises(
            ValueError,
            match="cannot start routes before snapshot publication",
        ):
            state.begin_route_runs(run_id, ("pdf",))

        assert state.route_run_count(run_id) == 0


def test_candidate_count_mismatch_does_not_publish_partial_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    snapshot = FileSnapshot(str(tmp_path / "one.pdf"), 1, 2, 3, 4, 5)
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(
            tmp_path,
            PortableInventoryCursor("C:", 1, 10),
        )
        state.store_route_candidates(
            run_id,
            (("application/pdf", snapshot),),
        )

        with pytest.raises(ValueError, match="candidate count changed"):
            state.publish_initial_routing_snapshot(
                run_id,
                7,
                0,
                1,
                "full",
                2,
            )

        row = state._connection.execute(
            "SELECT scan_id FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        event_count = state._connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND phase='routing-snapshot'",
            (run_id,),
        ).fetchone()[0]
        assert row == (None,)
        assert event_count == 0
        assert state.publish_initial_routing_snapshot(
            run_id,
            7,
            0,
            1,
            "full",
            1,
        )


@pytest.mark.parametrize(
    "details_json",
    (
        "{",
        "[]",
        json.dumps(
            {
                "schema": "neocortex.inventory-prepared/v2",
                "mode": "full",
                "scan_id": 1,
                "files": 1,
                "reconciliation_records": 0,
                "attempts": 1,
            }
        ),
        json.dumps(
            {
                "schema": "neocortex.inventory-prepared/v1",
                "mode": "full",
                "scan_id": 1,
                "files": 1,
                "reconciliation_records": 0,
                "attempts": True,
            }
        ),
    ),
    ids=("invalid-json", "non-object", "unsupported-schema", "boolean-attempts"),
)
def test_malformed_inventory_events_are_not_recovery_evidence(
    tmp_path: Path,
    details_json: str,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(
            tmp_path,
            PortableInventoryCursor("C:", 1, 10),
        )
        with state._connection:
            state._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info','inventory','Inventario preparado',?)""",
                (run_id, time.time_ns(), details_json),
            )

        with pytest.raises(ValueError, match="malformed inventory event"):
            state.recorded_inventory_evidence(run_id)


def test_conflicting_inventory_events_are_not_recovery_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(
            tmp_path,
            PortableInventoryCursor("C:", 1, 10),
        )
        first = {
            "schema": "neocortex.inventory-prepared/v1",
            "mode": "full",
            "scan_id": 7,
            "files": 1,
            "reconciliation_records": 0,
            "attempts": 1,
        }
        state.record_event(
            run_id,
            "info",
            "inventory",
            "Inventario preparado",
            first,
        )
        state.record_event(
            run_id,
            "info",
            "inventory",
            "Inventario preparado",
            {**first, "files": 2},
        )

        with pytest.raises(ValueError, match="ambiguous inventory event evidence"):
            state.recorded_inventory_evidence(run_id)


def test_routing_snapshot_publication_is_idempotent_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(
            tmp_path,
            PortableInventoryCursor("C:", 1, 10),
        )
        published = (7, 2, 0, "incremental", 0)

        assert state.publish_initial_routing_snapshot(run_id, *published)
        assert not state.publish_initial_routing_snapshot(run_id, *published)
        for conflict in (
            (8, 2, 0, "incremental", 0),
            (7, 3, 0, "incremental", 0),
            (7, 2, 1, "incremental", 0),
            (7, 2, 0, "full", 0),
        ):
            with pytest.raises(
                ValueError,
                match="conflicting routing snapshot metadata",
            ):
                state.publish_initial_routing_snapshot(run_id, *conflict)

        marker_count = state._connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND phase='routing-snapshot'",
            (run_id,),
        ).fetchone()[0]
        row = state._connection.execute(
            """SELECT scan_id,reconciliation_records,inventory_attempts,
            inventory_mode FROM initial_runs WHERE run_id=?""",
            (run_id,),
        ).fetchone()
    assert marker_count == 1
    assert row == published[:4]


@pytest.mark.parametrize(
    ("tamper", "message"),
    (
        ("root_identity", "belongs to a replaced corpus root"),
        ("file_count", "has inconsistent durable file counts"),
    ),
)
def test_route_only_rejects_inconsistent_bound_inventory_scan(
    tmp_path: Path,
    tamper: str,
    message: str,
) -> None:
    corpus = tmp_path / "corpus"
    state_dir = tmp_path / "state"
    corpus.mkdir()
    state_dir.mkdir()
    framework_database = state_dir / "framework.sqlite3"
    source_run = _source_run(framework_database, corpus)
    with closing(sqlite3.connect(framework_database)) as connection, connection:
        scan_id = int(
            connection.execute(
                "SELECT scan_id FROM initial_runs WHERE run_id=?",
                (source_run,),
            ).fetchone()[0]
        )
    with closing(sqlite3.connect(state_dir / "dedup.sqlite3")) as connection, connection:
        if tamper == "root_identity":
            connection.execute(
                "UPDATE scans SET root_file_id=? WHERE scan_id=?",
                (b"\xff" * 8, scan_id),
            )
        else:
            connection.execute(
                "UPDATE scans SET files_seen=files_seen+1 WHERE scan_id=?",
                (scan_id,),
            )

    executed = False

    def execute(_context):
        nonlocal executed
        executed = True
        return {"processed": 0}

    with pytest.raises(ValueError, match=message):
        FrameworkOrchestrator(
            FrameworkConfig(
                root=corpus,
                state_directory=state_dir,
                route="probe",
                route_only=True,
                candidate_run_id=source_run,
            ),
            route_registry={"probe": RouteAdapter("probe", execute)},
        ).run()
    assert executed is False
    with closing(sqlite3.connect(framework_database)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM initial_runs").fetchone()[0] == 1




# endregion [02]


# region [03] Read-only status and selection


def test_status_reports_stale_dead_owner_without_writing(tmp_path) -> None:
    corpus = tmp_path / "corpus"
    state = tmp_path / "state"
    corpus.mkdir()
    state.mkdir()
    database = state / "framework.sqlite3"
    run_id = _source_run(database, corpus)
    stale = time.time_ns() - 120_000_000_000
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """UPDATE initial_runs SET status='running',completed_ns=NULL,
            owner_pid=2147483647,heartbeat_ns=?,current_phase='derived'
            WHERE run_id=?""",
            (stale, run_id),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = database.read_bytes()
    status = list_run_status(database, run_id=run_id, limit=1)[0]
    after = database.read_bytes()
    assert status.current_phase == "derived"
    assert status.owner_alive is False
    assert status.heartbeat_stale is True
    assert before == after


def test_framework_selection_streams_path_and_review_intersection(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    first = FileSnapshot(str(tmp_path / "first.pdf"), 1, 1, 1, 1, 1)
    second = FileSnapshot(str(tmp_path / "second.pdf"), 1, 2, 1, 1, 1)
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(tmp_path, PortableInventoryCursor("C:", 1, 1))
        state.store_route_candidates(
            run_id,
            (
                ("application/pdf", first),
                ("application/pdf", second),
            ),
        )
        with state._connection:
            state._connection.execute(
                """INSERT INTO findings(
                route_name,volume_id,file_id,reason_code,path,size,mtime_ns,
                birthtime_ns,source_status,recommendation,retryable,confidence,
                evidence_json,detector_version,status,first_detected_ns,
                last_detected_ns,last_seen_run_id)
                VALUES('pdf','1','2','retry_test',?,1,1,1,'error','retry',1,
                1.0,'{}','test','open',1,1,?)""",
                (second.path, run_id),
            )
    route_state = FrameworkRouteState(database)
    selection = CandidateSelection.from_values(
        recommendations=("retry",),
        paths=(second.path,),
    )
    assert route_state.selected_route_candidate_counts(
        run_id,
        "application/pdf",
        0,
        "pdf",
        selection,
    ) == (1, 0)
    rows = list(
        route_state.iter_selected_route_candidates(
            run_id,
            "application/pdf",
            "pdf",
            selection,
        )
    )
    assert rows == [second]


# endregion [03]
