"""Adversarial coverage for the durable inventory/mutation boundary."""
# region [00] Contexto del módulo
# Módulo: tests/test_durable_mutation_boundary.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import neocortex.persistence.framework_state_common as state_common_module
import neocortex.integrations.inventory.inventory_boundary as boundary_module
import neocortex.runtime.orchestration.orchestrator as orchestrator_module
import neocortex.runtime.control.watcher as watcher_module
from tests.portable_inventory import PortableInventoryCursor
from neocortex.safety.internal_paths import InternalPathProtectionError
from neocortex.integrations.inventory.inventory_boundary import (
    NormalInventoryBoundary,
    build_normal_inventory_boundary,
)
from neocortex.safety.protected_content import (
    ProtectedContentPolicy,
    ProtectedPathSpec,
)
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import disjoint_internal_paths_policy
# endregion [01]

# region [02] Implementación


def _normal_run(
    state: FrameworkState,
    root: Path,
    state_directory: Path,
    *,
    boundary: NormalInventoryBoundary | None = None,
    signature: str | None = None,
) -> tuple[int, NormalInventoryBoundary]:
    effective = boundary or build_normal_inventory_boundary(root, state_directory)
    run_id = state.begin_initial_run(
        root,
        PortableInventoryCursor(root.drive, 1, 0),
        inventory_policy_signature=(
            effective.effective_signature if signature is None else signature
        ),
    )
    return run_id, effective


def test_shared_boundary_exports_have_identical_outputs(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    direct = boundary_module.build_normal_inventory_boundary(root, state_directory)
    reexported = orchestrator_module.build_normal_inventory_boundary(
        root,
        state_directory,
    )

    assert orchestrator_module.build_normal_inventory_boundary is (
        boundary_module.build_normal_inventory_boundary
    )
    assert watcher_module.build_normal_inventory_boundary is (
        boundary_module.build_normal_inventory_boundary
    )
    assert orchestrator_module.NormalInventoryBoundary is (boundary_module.NormalInventoryBoundary)
    assert direct.exclusion_policy == reexported.exclusion_policy
    assert direct.effective_signature == reexported.effective_signature


def test_normal_durable_boundary_allows_matching_run_and_action(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"

    with FrameworkState(database) as state:
        run_id, boundary = _normal_run(state, root, state_directory)
        guard = state.corpus_mutation_guard(run_id)
        guard.require_paths_allowed(root / "source.bin", root / "target.bin")
        action_id = state.begin_file_action(
            run_id,
            "fixture",
            str(root / "source.bin"),
            str(root / "target.bin"),
            None,
            None,
            True,
        )
        state.mark_file_actions_applying(((action_id, "{}"),))

        assert guard.policy == boundary.access_policy
        assert state._connection.execute(
            "SELECT status FROM main.file_actions WHERE action_id=?",
            (action_id,),
        ).fetchone() == ("applying",)

    assert FrameworkRouteState(database).corpus_mutation_guard(run_id).policy.root == root


@pytest.mark.parametrize("signature", (None, "mismatched-policy"))
def test_normal_null_or_mismatched_signature_fails_closed(
    tmp_path: Path,
    signature: str | None,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    boundary = build_normal_inventory_boundary(root, state_directory)

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        if signature is None:
            run_id = state.begin_initial_run(root, PortableInventoryCursor(root.drive, 1, 0))
        else:
            run_id, _ = _normal_run(
                state,
                root,
                state_directory,
                boundary=boundary,
                signature=signature,
            )
        with pytest.raises(InternalPathProtectionError):
            state.corpus_mutation_guard(run_id)


def test_copied_database_rejects_changed_state_owner(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    original_state = tmp_path / "state-a"
    copied_state = tmp_path / "state-b"
    root.mkdir()
    original_state.mkdir()
    copied_state.mkdir()
    original_database = original_state / "framework.sqlite3"

    with FrameworkState(original_database) as state:
        run_id, _ = _normal_run(state, root, original_state)
        assert state.corpus_mutation_guard(run_id).policy.root == root

    copied_database = copied_state / "framework.sqlite3"
    shutil.copy2(original_database, copied_database)
    with FrameworkState(copied_database, existing_only=True) as copied:
        with pytest.raises(
            InternalPathProtectionError,
            match="does not own the main database",
        ):
            copied.corpus_mutation_guard(run_id)


def test_changed_internal_policy_invalidates_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    initial_internal = disjoint_internal_paths_policy(tmp_path)
    boundary = build_normal_inventory_boundary(
        root,
        state_directory,
        internal_paths_policy=initial_internal,
    )
    monkeypatch.setattr(
        state_common_module,
        "canonical_internal_paths_policy",
        lambda: initial_internal,
    )

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id, _ = _normal_run(
            state,
            root,
            state_directory,
            boundary=boundary,
        )
        assert state.corpus_mutation_guard(run_id).policy.root == root
        repository = next(
            entry.configured_path
            for entry in initial_internal.entries
            if entry.role == "repository"
        )
        repository.mkdir(parents=True)
        changed_internal = disjoint_internal_paths_policy(tmp_path)
        monkeypatch.setattr(
            state_common_module,
            "canonical_internal_paths_policy",
            lambda: changed_internal,
        )

        with pytest.raises(
            InternalPathProtectionError,
            match="signature does not match",
        ):
            state.corpus_mutation_guard(run_id)


def test_changed_protected_policy_invalidates_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    protected_root = tmp_path / "read-only"
    root.mkdir()
    state_directory.mkdir()
    protected_root.mkdir()
    internal_policy = disjoint_internal_paths_policy(tmp_path)
    initial_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "read-only",
                "tree",
                "analyze_read_only",
                protected_root,
            ),
        )
    )
    changed_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "future-exclusion",
                "tree",
                "exclude",
                tmp_path / "future-protected",
            ),
            ProtectedPathSpec(
                "read-only",
                "tree",
                "analyze_read_only",
                protected_root,
            ),
        )
    )
    boundary = build_normal_inventory_boundary(
        root,
        state_directory,
        internal_paths_policy=internal_policy,
        protected_content_policy=initial_policy,
    )
    monkeypatch.setattr(
        state_common_module,
        "canonical_internal_paths_policy",
        lambda: internal_policy,
    )
    monkeypatch.setattr(
        state_common_module,
        "canonical_protected_content_policy",
        lambda: initial_policy,
    )

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id, _ = _normal_run(
            state,
            root,
            state_directory,
            boundary=boundary,
        )
        guard = state.corpus_mutation_guard(run_id)
        assert guard.protected_content_policy == initial_policy

        monkeypatch.setattr(
            state_common_module,
            "canonical_protected_content_policy",
            lambda: changed_policy,
        )
        with pytest.raises(
            InternalPathProtectionError,
            match="signature does not match",
        ):
            state.corpus_mutation_guard(run_id)


def test_file_action_snapshot_mismatch_never_crosses_frontier(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id, _ = _normal_run(state, root, state_directory)
        action_id = state.begin_file_action(
            run_id,
            "fixture",
            str(root / "source.bin"),
            None,
            None,
            None,
            True,
        )
        state._connection.execute("DROP TRIGGER main.file_actions_corpus_policy_no_update")
        state._connection.execute(
            "UPDATE main.file_actions SET protected_root=? WHERE action_id=?",
            (str(root), action_id),
        )
        state._connection.commit()

        with pytest.raises(
            InternalPathProtectionError,
            match="snapshot does not match",
        ):
            state.mark_file_actions_applying(((action_id, "{}"),))

        assert state._connection.execute(
            "SELECT status,expected_identity_json FROM main.file_actions WHERE action_id=?",
            (action_id,),
        ).fetchone() == ("started", None)
        assert state._connection.execute(
            "SELECT COUNT(*) FROM main.file_action_events WHERE action_id=?",
            (action_id,),
        ).fetchone() == (1,)


def test_temp_shadow_tables_cannot_divert_mutation_frontier(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id, _ = _normal_run(state, root, state_directory)
        action_id = state.begin_file_action(
            run_id,
            "fixture",
            str(root / "source.bin"),
            None,
            None,
            None,
            True,
        )
        state._connection.executescript(
            """
            CREATE TEMP TABLE initial_runs(marker TEXT);
            CREATE TEMP TABLE file_actions(marker TEXT);
            CREATE TEMP TABLE file_action_events(marker TEXT);
            """
        )

        state.mark_file_actions_applying(((action_id, "{}"),))

        assert state._connection.execute(
            "SELECT status FROM main.file_actions WHERE action_id=?",
            (action_id,),
        ).fetchone() == ("applying",)
        assert state._connection.execute(
            "SELECT to_status,stage FROM main.file_action_events "
            "WHERE action_id=? ORDER BY event_id",
            (action_id,),
        ).fetchall() == [
            ("started", "intent_recorded"),
            ("applying", "mutation_frontier"),
        ]
        assert state._connection.execute(
            "SELECT COUNT(*) FROM temp.file_action_events"
        ).fetchone() == (0,)


def test_preview_action_cannot_cross_mutation_frontier(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id, _ = _normal_run(state, root, state_directory)
        action_id = state.begin_file_action(
            run_id,
            "preview-fixture",
            str(root / "source.bin"),
            None,
            None,
            None,
            False,
        )

        with pytest.raises(
            InternalPathProtectionError,
            match="not explicitly authorized for apply",
        ):
            state.mark_file_actions_applying(((action_id, "{}"),))

        assert state._connection.execute(
            "SELECT apply_requested,status,expected_identity_json "
            "FROM main.file_actions WHERE action_id=?",
            (action_id,),
        ).fetchone() == (0, "started", None)
        assert state._connection.execute(
            "SELECT to_status,stage FROM main.file_action_events "
            "WHERE action_id=? ORDER BY event_id",
            (action_id,),
        ).fetchall() == [("started", "intent_recorded")]


def test_mark_applying_batch_rolls_back_if_one_action_policy_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id, _ = _normal_run(state, root, state_directory)
        first_id = state.begin_file_action(
            run_id,
            "fixture-first",
            str(root / "first.bin"),
            None,
            None,
            None,
            True,
        )
        second_id = state.begin_file_action(
            run_id,
            "fixture-second",
            str(root / "second.bin"),
            None,
            None,
            None,
            True,
        )
        state._connection.execute("DROP TRIGGER main.file_actions_corpus_policy_no_update")
        state._connection.execute(
            "UPDATE main.file_actions SET protected_root=? WHERE action_id=?",
            (str(root), second_id),
        )
        state._connection.commit()

        with pytest.raises(InternalPathProtectionError):
            state.mark_file_actions_applying(((first_id, "{}"), (second_id, "{}")))

        assert state._connection.execute(
            "SELECT action_id,status,expected_identity_json "
            "FROM main.file_actions ORDER BY action_id"
        ).fetchall() == [
            (first_id, "started", None),
            (second_id, "started", None),
        ]
        assert state._connection.execute(
            "SELECT action_id,to_status FROM main.file_action_events ORDER BY event_id"
        ).fetchall() == [
            (first_id, "started"),
            (second_id, "started"),
        ]


@pytest.mark.parametrize(
    ("column", "value"),
    (("root", "relative-root"), ("state_directory", "")),
)
def test_relative_or_empty_persisted_paths_fail_closed(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id, _ = _normal_run(state, root, state_directory)
        state._connection.execute("DROP TRIGGER main.initial_runs_corpus_policy_no_update")
        state._connection.execute(
            f"UPDATE main.initial_runs SET {column}=? WHERE run_id=?",
            (value, run_id),
        )
        state._connection.commit()

        with pytest.raises(InternalPathProtectionError):
            state.corpus_mutation_guard(run_id)




def test_in_memory_main_database_is_never_a_durable_owner(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    with FrameworkState(":memory:") as state:
        boundary = build_normal_inventory_boundary(root, state_directory)
        run_id = state.begin_initial_run(
            root,
            PortableInventoryCursor(root.drive, 1, 0),
            inventory_policy_signature=boundary.effective_signature,
        )
        with pytest.raises(
            InternalPathProtectionError,
            match="not a durable file",
        ):
            state.corpus_mutation_guard(run_id)


# endregion [02]
