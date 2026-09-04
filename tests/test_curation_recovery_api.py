"""Fixed-root recovery status/preview and restore confirmation tests."""

from __future__ import annotations

import sqlite3

from neocortex.api.curation_recovery_api import (
    curation_recovery_status_payload,
    curation_restore_payload,
    curation_restore_preview_payload,
)
from neocortex.curation.application import apply_authorization_grant
from neocortex.curation.recovery import PosixRestoreBackend, restore_curation_preview
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run
from tests.test_curation_application import FixtureTrashBackend, _fixture


def test_restore_preview_is_read_only_and_returns_exact_confirmation(tmp_path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        result = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=FixtureTrashBackend(trash, structured=True),
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
        action_id = result.effects[0].action_id
        assert action_id is not None
    before = framework.read_bytes()
    payload = curation_restore_preview_payload(
        action_id,
        state_directory=state,
        database=framework,
        request_id="restore-preview",
    )
    assert payload["status"] == "complete"
    assert payload["result"]["restorable"] is True  # type: ignore[index]
    assert framework.read_bytes() == before


def test_public_restore_requires_backend_and_recovery_status_is_bounded(tmp_path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        result = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=FixtureTrashBackend(trash, structured=True),
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
        action_id = result.effects[0].action_id
        assert action_id is not None
    preview = restore_curation_preview(framework, action_id)
    blocked = curation_restore_payload(
        action_id,
        confirm_action_id=action_id,
        confirmation=str(preview["confirmation"]),
        actor="victor",
        state_directory=state,
        database=framework,
        request_id="restore-no-backend",
    )
    assert blocked["status"] == "unavailable"
    assert blocked["error"]["code"] == "backend_unavailable"  # type: ignore[index]
    status = curation_recovery_status_payload(
        state_directory=state,
        database=framework,
        limit=10,
        request_id="recovery-status",
    )
    assert status["status"] == "complete"
    assert status["result"]["count"] == 0  # type: ignore[index]
    with sqlite3.connect(framework) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM file_actions WHERE action_type='restore_curation'"
        ).fetchone() == (0,)


def test_public_restore_adapter_reuses_fixed_root_and_confirmation(tmp_path) -> None:
    state, corpus, trash, framework, grant_id = _fixture(tmp_path)
    with FrameworkState(framework) as framework_state:
        run_id = begin_signed_normal_run(framework_state, corpus)
        result = apply_authorization_grant(
            state,
            framework,
            grant_id,
            run_id=run_id,
            backend=FixtureTrashBackend(trash, structured=True),
            state=framework_state,
            clock_ns=lambda: 4_000,
        )
        action_id = result.effects[0].action_id
        assert action_id is not None
    preview = restore_curation_preview(framework, action_id)
    restored = curation_restore_payload(
        action_id,
        confirm_action_id=action_id,
        confirmation=str(preview["confirmation"]),
        actor="victor",
        backend=PosixRestoreBackend(trash),
        state_directory=state,
        database=framework,
        request_id="restore-api",
    )
    assert restored["status"] == "restored"
    assert restored["result"]["idempotent"] is False  # type: ignore[index]
    assert len(list(corpus.iterdir())) == 2
