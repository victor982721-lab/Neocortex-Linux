"""Reset scopes must not discard unresolved filesystem-effect evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.state_reset import (
    STATE_RESET_CONFIRMATION,
    StateResetBusyError,
    execute_state_reset,
    plan_state_reset,
)
from tests.internal_paths_test_support import begin_signed_normal_run


def test_cache_scope_blocks_recovery_required_actions(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "framework.sqlite3"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    with FrameworkState(database) as framework:
        run_id = begin_signed_normal_run(framework, corpus)
        framework.fail_initial_run(run_id)
        action_id = framework.begin_file_action(
            run_id,
            "correct_extension",
            str(corpus / "source.fixture"),
            str(corpus / "target.fixture"),
            None,
            "controlled fixture",
            True,
        )
        framework.require_file_action_recovery((action_id,), "uncertain fixture")

    plan = plan_state_reset(state, scope="runs-and-caches")
    assert plan.active_action_ids == (action_id,)
    with pytest.raises(StateResetBusyError):
        execute_state_reset(
            state,
            scope="runs-and-caches",
            apply=True,
            plan_digest=plan.plan_digest,
            confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "backup",
        )
    assert database.is_file()
