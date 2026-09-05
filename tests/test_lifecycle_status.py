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

