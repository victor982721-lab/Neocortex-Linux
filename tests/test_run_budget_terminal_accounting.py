"""Terminal budgets measure execution, never the age of a later status query."""

from pathlib import Path

import pytest

from neocortex.persistence import framework_state_writer
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
from neocortex.runtime.orchestration.run_status import list_run_status


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
@pytest.mark.parametrize("finish_after_deadline", [False, True])
def test_terminal_budget_is_frozen_in_writer_and_public_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    finish_after_deadline: bool,
) -> None:
    clock = [100_000_000_000]
    monkeypatch.setattr(framework_state_writer.time, "time_ns", lambda: clock[0])
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(tmp_path, None)
        run_start = clock[0]
        clock[0] += 29_286_000
        state.publish_run_budget(run_id, {"max_duration_seconds": 60.0})
        budget_start = clock[0]
        state.reserve_run_budget(run_id, "fixture", items=2, bytes=80)
        clock[0] += (61 if finish_after_deadline else 2) * 1_000_000_000
        completion = clock[0]
        with state._connection:
            state._connection.execute(
                "UPDATE initial_runs SET status=?,completed_ns=? WHERE run_id=?",
                (status, completion, run_id),
            )
        first = state.read_run_budget(run_id)
        clock[0] += 300_000_000_000
        second = state.read_run_budget(run_id)
        assert first == second
        assert second is not None
        assert second["started_ns"] == budget_start
        assert second["elapsed_ns"] == completion - budget_start
        assert second["elapsed_until_ns"] == completion
        assert second["elapsed_scope"] == "budget_start_to_run_completion"
        assert second["consumed_bytes_kind"] == "reserved_input_bytes_not_physical_io"
        assert second["expired"] is finish_after_deadline
        assert state.reserve_run_budget(run_id, "fixture", items=2, bytes=80)["replayed"]
        with pytest.raises(RunBudgetExceeded, match="terminal"):
            state.reserve_run_budget(run_id, "new", items=1)
        with pytest.raises(RunBudgetExceeded, match="terminal"):
            state.check_run_budget(run_id)

    first_status = list_run_status(database, run_id=run_id)[0]
    clock[0] += 300_000_000_000
    second_status = list_run_status(database, run_id=run_id)[0]
    assert first_status.budget == second_status.budget
    assert first_status.elapsed_ns == second_status.elapsed_ns == completion - run_start
    assert first_status.budget is not None
    for key in (
        "elapsed_seconds", "elapsed_ns", "elapsed_until_ns", "elapsed_scope",
        "started_ns", "expired", "consumed_bytes", "consumed_bytes_kind",
    ):
        assert first_status.budget[key] == second[key]


def test_running_budget_still_measures_observation_and_enforces_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100_000_000_000]
    monkeypatch.setattr(framework_state_writer.time, "time_ns", lambda: clock[0])
    with FrameworkState(tmp_path / "framework.sqlite3") as state:
        run_id = state.begin_initial_run(tmp_path, None)
        state.publish_run_budget(run_id, {"max_duration_seconds": 60.0})
        first = state.read_run_budget(run_id)
        clock[0] += 61_000_000_000
        second = state.read_run_budget(run_id)
        assert first is not None and second is not None
        assert second["elapsed_seconds"] - first["elapsed_seconds"] == 61.0
        assert second["elapsed_scope"] == "budget_start_to_observation"
        assert second["expired"] is True
        with pytest.raises(RunBudgetExceeded, match="time"):
            state.reserve_run_budget(run_id, "late")
