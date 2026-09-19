from __future__ import annotations

from pathlib import Path

from neocortex.api.lifecycle_read_api import lifecycle_status_payload
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.orchestration.run_manifest import RunManifest


def test_lifecycle_status_payload_is_bounded_read_only(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = state_dir / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(root),
                root_identity=(1, 2, -1),
                selected_routes=("text",),
            ).event_payload(),
        )

    payload = lifecycle_status_payload(limit=5, state_directory=state_dir)
    assert payload["read_only"] is True
    assert payload["schema"] == "neocortex.lifecycle-envelope/v1"
    assert payload["kind"] == "neocortex_lifecycle_status"
    assert payload["result"]["count"] == 1
    assert payload["runs"][0]["manifest"]["schema"] == "neocortex.run-manifest/v1"


def test_cancelled_run_with_stage_and_checkpoint_remains_publicly_readable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = state_dir / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(root),
                root_identity=(1, 2, -1),
                selected_routes=("text",),
            ).event_payload(),
        )
        state.publish_run_stage(
            run_id,
            "semantic",
            "pending",
            details={"selected_sources": []},
            idempotency_key="semantic:pending",
            checkpoint={"cursor": "before-work"},
        )
        assert state.cancel_initial_run(run_id)

    payload = lifecycle_status_payload(limit=5, state_directory=state_dir)
    assert payload["coverage"] == "complete"
    assert payload["status"] == "ok"
    run = payload["runs"][0]
    assert run["status"] == "cancelled"
    assert run["stages"][0]["idempotency_key"] == "semantic:pending"
    assert run["checkpoints"][0]["idempotency_key"] == "semantic:pending:checkpoint"


def test_cancelled_run_without_manifest_keeps_its_run_id_in_public_envelope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with FrameworkState(state_dir / "framework.sqlite3") as state:
        run_id = state.begin_initial_run(root, None)
        assert state.cancel_initial_run(run_id)

    payload = lifecycle_status_payload(limit=5, state_directory=state_dir)
    assert payload["coverage"] == "complete"
    assert payload["status"] == "ok"
    assert payload["runs"][0]["run_id"] == run_id
    assert payload["runs"][0]["lifecycle"]["run_id"] == run_id


def test_lifecycle_stage_metadata_accepts_unavailable_birthtime_sentinel(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with FrameworkState(state_dir / "framework.sqlite3") as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(root),
                root_identity=(1, 2, -1),
                selected_routes=("text",),
            ).event_payload(),
        )
        state.publish_run_stage(
            run_id,
            "organization_plan",
            "pending",
            details={"root_identity": [1, 2, -1]},
        )
        state.cancel_initial_run(run_id)

    payload = lifecycle_status_payload(limit=5, state_directory=state_dir)
    assert payload["coverage"] == "complete"
    assert payload["status"] == "ok"
    assert payload["runs"][0]["stages"][0]["details"]["root_identity"] == [1, 2, -1]
