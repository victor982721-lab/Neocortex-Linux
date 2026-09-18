"""Cross-owner run provenance is retained by the runs-only scope."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from neocortex.capabilities.formats.image.state import initialize_image_state
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.state_reset import (
    STATE_RESET_CONFIRMATION,
    execute_state_reset,
    plan_state_reset,
)


def test_runs_scope_keeps_framework_parent_named_by_image_cache(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    framework = state / "framework.sqlite3"
    with FrameworkState(framework):
        pass
    with closing(sqlite3.connect(framework)) as connection, connection:
        for run_id in (10, 20):
            connection.execute(
                "INSERT INTO initial_runs(run_id,root,started_ns,status) "
                "VALUES(?,?,?,'completed')",
                (run_id, "/tmp/corpus", run_id),
            )

    image = state / "image.sqlite3"
    initialize_image_state(image)
    with closing(sqlite3.connect(image)) as connection, connection:
        connection.execute(
            """INSERT INTO images(
                file_key,path,mime,size,mtime_ns,birthtime_ns,last_seen_run_id,status
            ) VALUES(?,?,?,?,?,?,?,'pending')""",
            ("fixture", "/tmp/image.png", "image/png", 1, 1, -1, 10),
        )

    preview = plan_state_reset(state, scope="runs")
    assert preview.cross_owner_run_ids == (10,)
    assert preview.framework_plan is not None
    assert preview.framework_plan.delete_run_ids == (20,)
    assert preview.framework_plan.retained_run_ids == (10,)

    result = execute_state_reset(
        state,
        scope="runs",
        apply=True,
        plan_digest=preview.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
        backup_directory=tmp_path / "backup",
    )
    assert result.framework_result is not None
    with sqlite3.connect(framework) as connection:
        assert connection.execute("SELECT run_id FROM initial_runs").fetchall() == [(10,)]
    with sqlite3.connect(image) as connection:
        assert connection.execute("SELECT last_seen_run_id FROM images").fetchall() == [(10,)]
