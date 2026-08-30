# region [00] Contexto del módulo
# Módulo: tests/test_framework_actions_identity_bound.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.enumeration import JournalCursor
from neocortex.deduplication import DedupIndex, DedupPlanner, snapshot_path
from neocortex.workflow.actions import actions as actions_module
from neocortex.persistence import framework_state_common as state_common_module
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.platform.content_types import DetectedType
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
    ProtectedAnalysisRootError,
)
from neocortex.safety.internal_paths import (
    InternalPathProtectionError,
    InternalPathSpec,
    InternalPathsPolicy,
)
from neocortex.runtime.models import ActionSummary
from neocortex.safety.protected_content import (
    ProtectedContentPolicy,
    ProtectedPathSpec,
)
from neocortex.workflow.self_analysis.self_analysis import (
    build_self_analysis_inventory_policy,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.safety.windows_handle_mutation import (
    rename_no_replace_by_identity,
)
from tests.internal_paths_test_support import (
    begin_signed_normal_run,
    disjoint_internal_paths_policy,
)
from tests.mutation_containment import ContainedMutationRoot
# endregion [01]

# region [02] Implementación


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="identity-bound framework actions require Windows"
)


@pytest.fixture
def mutation_containment(tmp_path: Path) -> Iterator[ContainedMutationRoot]:
    base = tmp_path / "native-mutation-roots"
    base.mkdir()
    containment = ContainedMutationRoot.create(base, watch_directories=(base,))
    yield containment
    containment.assert_no_leaks()


def _begin_normal_run(
    state: FrameworkState,
    root: Path,
    *,
    internal_paths_policy: InternalPathsPolicy | None = None,
) -> int:
    return begin_signed_normal_run(
        state,
        root,
        internal_paths_policy=internal_paths_policy,
    )


def _normal_state_database(sandbox: Path) -> Path:
    state_directory = sandbox / "state"
    state_directory.mkdir()
    return state_directory / "framework.sqlite3"


def _protected_mutation_guard(
    root: Path,
    protected_root: Path,
) -> CorpusMutationGuard:
    return CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", root),
        disjoint_internal_paths_policy(root),
        ProtectedContentPolicy.capture(
            (
                ProtectedPathSpec(
                    "fixture_read_only",
                    "tree",
                    "analyze_read_only",
                    protected_root,
                ),
            )
        ),
    )


def test_protected_extension_mismatch_is_analyzed_without_action_or_syscall(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = mutation_containment.root
    corpus = sandbox / "corpus"
    protected_root = corpus / "read-only"
    protected_root.mkdir(parents=True)
    source = protected_root / "image.txt"
    source.write_bytes(b"protected-object")
    planned = snapshot_path(source)
    detected = DetectedType("image/png", ".png", frozenset({".png"}), "fixture")
    native_calls = 0

    def forbidden_native(*_args: object, **_kwargs: object) -> None:
        nonlocal native_calls
        native_calls += 1
        raise AssertionError("protected rename reached the native syscall")

    monkeypatch.setattr(actions_module, "detect_content_type", lambda _path: detected)
    monkeypatch.setattr(
        actions_module,
        "rename_no_replace_by_identity",
        forbidden_native,
    )
    with (
        DedupIndex(sandbox / "dedup.sqlite3") as index,
        FrameworkState(_normal_state_database(sandbox)) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = _begin_normal_run(state, corpus)
        guard = _protected_mutation_guard(corpus, protected_root)
        monkeypatch.setattr(
            state,
            "corpus_mutation_guard",
            lambda _run_id: guard,
        )
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )

        summary, route_candidate, cache_update = actions._inspect_content_type_candidate(
            planned,
            ActionSummary(apply_actions=True),
        )
        assert summary.files_checked == 1
        assert summary.types_detected == 1
        assert summary.rename_candidates == 1
        assert summary.rename_skips == 1
        assert route_candidate is not None
        assert route_candidate[1].path == str(source)
        assert cache_update == (planned, detected)
        assert state._connection.execute(
            "SELECT COUNT(*) FROM file_actions WHERE run_id=?",
            (run_id,),
        ).fetchone() == (0,)

    assert native_calls == 0
    assert source.read_bytes() == b"protected-object"
    assert not source.with_suffix(".png").exists()


@pytest.mark.parametrize("failure_point", ("snapshot", "detector"))
def test_protected_inspection_oserror_is_non_actionable_and_batch_continues(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    sandbox = mutation_containment.root
    corpus = sandbox / "corpus"
    protected_root = corpus / "read-only"
    protected_root.mkdir(parents=True)
    protected_source = protected_root / "a-protected.txt"
    safe_source = corpus / "z-safe.txt"
    protected_source.write_bytes(b"protected-inspection-object")
    safe_source.write_bytes(b"safe-mutable-object")
    safe_target = safe_source.with_suffix(".png")
    detected = DetectedType("image/png", ".png", frozenset({".png"}), "fixture")
    error_message = f"{failure_point} denied protected fixture"
    real_snapshot_path = actions_module.snapshot_path
    native_sources: list[Path] = []

    def snapshot_with_protected_failure(path: str | Path) -> object:
        if failure_point == "snapshot" and Path(path) == protected_source:
            raise OSError(error_message)
        return real_snapshot_path(path)

    def detect_with_protected_failure(path: str | Path) -> DetectedType:
        if failure_point == "detector" and Path(path) == protected_source:
            raise OSError(error_message)
        return detected

    def identity_bound_rename(
        source_path: Path,
        destination_path: Path,
        expected: object,
        **kwargs: object,
    ) -> object:
        native_sources.append(source_path)
        return mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source_path,
            destination_path,
            expected,  # type: ignore[arg-type]
            before_native_call=kwargs["before_native_call"],  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        actions_module,
        "snapshot_path",
        snapshot_with_protected_failure,
    )
    monkeypatch.setattr(
        actions_module,
        "detect_content_type",
        detect_with_protected_failure,
    )
    monkeypatch.setattr(
        actions_module,
        "rename_no_replace_by_identity",
        identity_bound_rename,
    )

    with (
        DedupIndex(sandbox / "dedup.sqlite3") as index,
        FrameworkState(_normal_state_database(sandbox)) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = _begin_normal_run(state, corpus)
        guard = _protected_mutation_guard(corpus, protected_root)
        monkeypatch.setattr(
            state,
            "corpus_mutation_guard",
            lambda _run_id: guard,
        )

        summary = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        ).execute(plan, cleanup_empty_directories=False)
        action_rows = [
            tuple(row)
            for row in state._connection.execute(
                """SELECT action_type,source_path,target_path,status
                FROM file_actions WHERE run_id=? ORDER BY action_id""",
                (run_id,),
            ).fetchall()
        ]
        observation = state._connection.execute(
            """SELECT level,phase,message,details_json FROM run_events
            WHERE run_id=? AND message='Protected content inspection failed'""",
            (run_id,),
        ).fetchone()

    assert summary.files_checked == 2
    assert summary.errors == 1
    assert summary.types_detected == 1
    assert summary.rename_candidates == 1
    assert summary.files_renamed == 1
    assert action_rows == [
        (
            "correct_extension",
            str(safe_source),
            str(safe_target),
            "applied",
        )
    ]
    assert observation is not None
    assert tuple(observation[:3]) == (
        "error",
        "content-types",
        "Protected content inspection failed",
    )
    details = json.loads(str(observation[3]))
    assert details["actionable"] is False
    assert details["error"] == error_message
    assert details["error_type"] == "OSError"
    assert details["path"] == str(protected_source)
    assert "fixture_read_only" in details["protected_reason"]
    assert native_sources == [safe_source]
    assert protected_source.read_bytes() == b"protected-inspection-object"
    assert not protected_source.with_suffix(".png").exists()
    assert not safe_source.exists()
    assert safe_target.read_bytes() == b"safe-mutable-object"


def test_mixed_trash_batch_filters_protected_source_before_action_rows(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = mutation_containment.root
    corpus = sandbox / "corpus"
    protected_root = corpus / "read-only"
    protected_root.mkdir(parents=True)
    protected_source = protected_root / "protected.bin"
    safe_source = corpus / "safe.bin"
    protected_source.write_bytes(b"protected")
    safe_source.write_bytes(b"safe")
    protected_snapshot = snapshot_path(protected_source)
    safe_snapshot = snapshot_path(safe_source)

    with (
        DedupIndex(sandbox / "dedup.sqlite3") as index,
        FrameworkState(_normal_state_database(sandbox)) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = _begin_normal_run(state, corpus)
        guard = _protected_mutation_guard(corpus, protected_root)
        guard_rebuilds = 0

        def load_guard(_run_id: int) -> CorpusMutationGuard:
            nonlocal guard_rebuilds
            guard_rebuilds += 1
            return guard

        monkeypatch.setattr(
            state,
            "corpus_mutation_guard",
            load_guard,
        )
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )

        result = actions._apply_trash_batch(
            "trash_duplicate",
            (
                (str(protected_source), "fixture=protected"),
                (str(safe_source), "fixture=safe"),
            ),
            expected_snapshots=(protected_snapshot, safe_snapshot),
        )
        rows = state._connection.execute(
            "SELECT source_path,status FROM file_actions WHERE run_id=?",
            (run_id,),
        ).fetchall()

    assert result == (0, 0, 2)
    assert guard_rebuilds == 1
    assert [tuple(row) for row in rows] == [(str(safe_source), "skipped")]
    assert protected_source.read_bytes() == b"protected"
    assert safe_source.read_bytes() == b"safe"


def test_protected_duplicate_keeper_remains_a_read_only_observation(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = mutation_containment.root
    corpus = sandbox / "corpus"
    protected_root = corpus / "read-only"
    protected_root.mkdir(parents=True)
    keeper = protected_root / "keeper.bin"
    redundant = corpus / "redundant.bin"
    keeper.write_bytes(b"same")
    redundant.write_bytes(b"same")
    keeper_snapshot = snapshot_path(keeper)
    redundant_snapshot = snapshot_path(redundant)

    with (
        DedupIndex(sandbox / "dedup.sqlite3") as index,
        FrameworkState(_normal_state_database(sandbox)) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = _begin_normal_run(state, corpus)
        guard = _protected_mutation_guard(corpus, protected_root)
        monkeypatch.setattr(
            state,
            "corpus_mutation_guard",
            lambda _run_id: guard,
        )
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )

        observed = actions._validate_trash_candidate(
            "trash_duplicate",
            str(redundant),
            redundant_snapshot,
            keeper_snapshot,
        )

    assert int(observed.st_ino) == redundant_snapshot.file_id


def test_apply_rejects_analyze_only_run_before_recording_an_action(
    mutation_containment: ContainedMutationRoot,
) -> None:
    sandbox = mutation_containment.root
    corpus = sandbox / "corpus"
    corpus.mkdir()
    source = corpus / "image.txt"
    source.write_bytes(b"protected-object")
    planned = snapshot_path(source)
    state_directory = sandbox / "state"
    state_directory.mkdir()
    policy = CorpusAccessPolicy.capture("analyze_only", corpus)
    detected = DetectedType("image/png", ".png", frozenset({".png"}), "fixture")

    with (
        DedupIndex(sandbox / "dedup.sqlite3") as index,
        FrameworkState(state_directory / "framework.sqlite3") as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        inventory_policy = build_self_analysis_inventory_policy(
            corpus,
            state_directory,
        )
        run_id = state.begin_self_analysis_run(
            policy,
            JournalCursor(corpus.drive, 1, 0),
            state_directory=state_directory,
            inventory_policy_signature=inventory_policy.signature,
        )
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )

        with pytest.raises(ProtectedAnalysisRootError):
            actions._rename_mismatch(
                planned,
                detected,
                ActionSummary(apply_actions=True),
            )
        assert state._connection.execute(
            "SELECT COUNT(*) FROM file_actions WHERE run_id=?",
            (run_id,),
        ).fetchone() == (0,)

    assert source.read_bytes() == b"protected-object"
    assert not source.with_suffix(".png").exists()


def test_apply_rejects_run_and_inventory_root_mismatch_before_action(
    mutation_containment: ContainedMutationRoot,
) -> None:
    sandbox = mutation_containment.root
    run_root = sandbox / "run-root"
    run_root.mkdir()
    scan_root = sandbox / "protected-scan-root"
    scan_root.mkdir()
    source = scan_root / "image.txt"
    source.write_bytes(b"protected-object")
    planned = snapshot_path(source)
    detected = DetectedType("image/png", ".png", frozenset({".png"}), "fixture")

    with (
        DedupIndex(sandbox / "dedup.sqlite3") as index,
        FrameworkState(_normal_state_database(sandbox)) as state,
    ):
        scan = index.scan(scan_root, excluded_paths=())
        run_id = _begin_normal_run(state, run_root)
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )

        with pytest.raises(RuntimeError, match="does not match the inventory scan"):
            actions._rename_mismatch(
                planned,
                detected,
                ActionSummary(apply_actions=True),
            )
        assert state._connection.execute(
            "SELECT COUNT(*) FROM file_actions WHERE run_id=?",
            (run_id,),
        ).fetchone() == (0,)

    assert source.read_bytes() == b"protected-object"
    assert not source.with_suffix(".png").exists()


def test_extension_action_blocks_source_swap_at_syscall_boundary(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = mutation_containment.root
    corpus = root / "corpus"
    corpus.mkdir()
    source = corpus / "image.txt"
    source.write_bytes(b"authorized-object")
    planned = snapshot_path(source)
    destination = source.with_suffix(".png")
    attempted_elsewhere = corpus / "authorized-away.txt"
    blocked: list[int] = []
    detected = DetectedType("image/png", ".png", frozenset({".png"}), "fixture")

    def identity_bound_with_race(
        source_path: Path,
        destination_path: Path,
        expected: object,
        **kwargs: object,
    ) -> object:
        def attempt_swap() -> None:
            try:
                mutation_containment.rename(source_path, attempted_elsewhere)
            except PermissionError as exc:
                blocked.append(int(exc.winerror or 0))

        return mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source_path,
            destination_path,
            expected,  # type: ignore[arg-type]
            before_native_call=kwargs["before_native_call"],  # type: ignore[arg-type]
            _before_native_call=attempt_swap,
        )

    monkeypatch.setattr(actions_module, "rename_no_replace_by_identity", identity_bound_with_race)
    database = _normal_state_database(root)
    with (
        DedupIndex(root / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = _begin_normal_run(state, corpus)
        summary = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )._rename_mismatch(planned, detected, ActionSummary(apply_actions=True))

    assert blocked
    assert summary.files_renamed == 1
    assert not source.exists()
    assert destination.read_bytes() == b"authorized-object"
    assert not attempted_elsewhere.exists()
    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM file_actions WHERE action_type='correct_extension'"
        ).fetchone()[0]
    assert status == "applied"


def test_mismatched_native_receipt_requires_recovery_before_confirmation(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = mutation_containment.root
    corpus = sandbox / "corpus"
    corpus.mkdir()
    source = corpus / "image.txt"
    source.write_bytes(b"authorized-object")
    planned = snapshot_path(source)
    destination = source.with_suffix(".png")
    detected = DetectedType("image/png", ".png", frozenset({".png"}), "fixture")

    def identity_bound_with_bad_receipt(
        source_path: Path,
        destination_path: Path,
        expected: object,
        **kwargs: object,
    ) -> object:
        receipt = mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source_path,
            destination_path,
            expected,  # type: ignore[arg-type]
            before_native_call=kwargs["before_native_call"],  # type: ignore[arg-type]
        )
        return replace(receipt, file_id=receipt.file_id + 1)

    monkeypatch.setattr(
        actions_module,
        "rename_no_replace_by_identity",
        identity_bound_with_bad_receipt,
    )
    database = _normal_state_database(sandbox)
    with (
        DedupIndex(sandbox / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = _begin_normal_run(state, corpus)
        summary = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )._rename_mismatch(planned, detected, ActionSummary(apply_actions=True))

    assert summary.files_renamed == 0
    assert not source.exists()
    assert destination.read_bytes() == b"authorized-object"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM file_actions WHERE action_type='correct_extension'"
        ).fetchone() == ("recovery_required",)


def test_competing_destination_is_preserved_and_fails_before_frontier(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = mutation_containment.root
    corpus = root / "corpus"
    corpus.mkdir()
    source = corpus / "image.txt"
    source.write_bytes(b"authorized-object")
    planned = snapshot_path(source)
    destination = source.with_suffix(".png")
    detected = DetectedType("image/png", ".png", frozenset({".png"}), "fixture")

    def identity_bound_with_collision(
        source_path: Path,
        destination_path: Path,
        expected: object,
        **kwargs: object,
    ) -> object:
        return mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source_path,
            destination_path,
            expected,  # type: ignore[arg-type]
            before_native_call=kwargs["before_native_call"],  # type: ignore[arg-type]
            _before_native_call=lambda: destination_path.write_bytes(b"competitor"),
        )

    monkeypatch.setattr(
        actions_module, "rename_no_replace_by_identity", identity_bound_with_collision
    )
    database = _normal_state_database(root)
    with (
        DedupIndex(root / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = _begin_normal_run(state, corpus)
        summary = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )._rename_mismatch(planned, detected, ActionSummary(apply_actions=True))

    assert summary.files_renamed == 0
    assert source.read_bytes() == b"authorized-object"
    assert destination.read_bytes() == b"competitor"
    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM file_actions WHERE action_type='correct_extension'"
        ).fetchone()[0]
    assert status == "failed"


def test_trash_validation_does_not_degrade_protected_root_error(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = mutation_containment.root
    corpus = sandbox / "corpus"
    corpus.mkdir()
    source = corpus / "source.bin"
    source.write_bytes(b"protected-object")
    planned = snapshot_path(source)

    with (
        DedupIndex(sandbox / "dedup.sqlite3") as index,
        FrameworkState(_normal_state_database(sandbox)) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = _begin_normal_run(state, corpus)
        action_id = state.begin_file_action(
            run_id,
            "trash_fixture",
            str(source),
            None,
            None,
            None,
            True,
        )
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )

        def reject_protected(*_args: object, **_kwargs: object) -> os.stat_result:
            raise ProtectedAnalysisRootError("simulated trash boundary")

        monkeypatch.setattr(
            actions,
            "_validate_trash_candidate",
            reject_protected,
        )
        with pytest.raises(ProtectedAnalysisRootError):
            actions._preflight_trash_candidates(
                "trash_fixture",
                [(action_id, str(source), planned, None)],
            )
        with pytest.raises(ProtectedAnalysisRootError):
            actions._revalidate_trash_candidates(
                "trash_fixture",
                [(action_id, str(source), planned, None, os.stat(source))],
            )
        assert state._connection.execute(
            "SELECT status FROM file_actions WHERE action_id=?",
            (action_id,),
        ).fetchone() == ("started",)


def test_retained_boundary_propagates_protected_root_error(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = mutation_containment.root
    corpus = sandbox / "corpus"
    corpus.mkdir()
    source = corpus / "image.txt"
    source.write_bytes(b"authorized-object")
    planned = snapshot_path(source)
    destination = source.with_suffix(".png")
    protected_guard = CorpusMutationGuard(
        CorpusAccessPolicy.capture("analyze_only", corpus),
        disjoint_internal_paths_policy(sandbox),
    )
    detected = DetectedType("image/png", ".png", frozenset({".png"}), "fixture")

    with (
        DedupIndex(sandbox / "dedup.sqlite3") as index,
        FrameworkState(_normal_state_database(sandbox)) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = _begin_normal_run(state, corpus)
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )

        def invoke_retained_boundary(
            _source_path: Path,
            _destination_path: Path,
            _expected: object,
            **kwargs: object,
        ) -> object:
            monkeypatch.setattr(
                state,
                "corpus_mutation_guard",
                lambda _run_id: protected_guard,
            )
            callback = kwargs.get("before_native_call")
            assert callable(callback)
            callback()
            raise AssertionError("native syscall would have been reached")

        monkeypatch.setattr(
            actions_module,
            "rename_no_replace_by_identity",
            invoke_retained_boundary,
        )
        with pytest.raises(ProtectedAnalysisRootError):
            actions._rename_mismatch(
                planned,
                detected,
                ActionSummary(apply_actions=True),
            )
        assert state._connection.execute(
            "SELECT status FROM file_actions WHERE action_type='correct_extension'"
        ).fetchone() == ("failed",)

    assert source.read_bytes() == b"authorized-object"
    assert not destination.exists()


def test_normal_internal_source_is_rejected_before_native_rename(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = mutation_containment.root
    corpus = sandbox / "profile"
    repository = corpus / "Neocortex" / "Repository"
    repository.mkdir(parents=True)
    source = repository / "image.txt"
    source.write_bytes(b"protected-source")
    planned = snapshot_path(source)
    destination = source.with_suffix(".png")
    runtime = sandbox / "Programs" / "Neocortex"
    application_data = sandbox / "AppData" / "Neocortex"
    internal_policy = InternalPathsPolicy.capture(
        (
            InternalPathSpec("repository", "tree", repository),
            InternalPathSpec("runtime", "tree", runtime),
            InternalPathSpec("application_data", "tree", application_data),
            InternalPathSpec(
                "self_analysis",
                "tree",
                application_data / "self-analysis",
            ),
            InternalPathSpec(
                "launcher",
                "file",
                runtime / "bin" / "Neocortex.exe",
            ),
        )
    )
    detected = DetectedType("image/png", ".png", frozenset({".png"}), "fixture")
    native_calls = 0

    def forbidden_native(*_args: object, **_kwargs: object) -> None:
        nonlocal native_calls
        native_calls += 1
        raise AssertionError("native rename must remain unreachable")

    monkeypatch.setattr(
        state_common_module,
        "canonical_internal_paths_policy",
        lambda: internal_policy,
    )
    monkeypatch.setattr(
        actions_module,
        "rename_no_replace_by_identity",
        forbidden_native,
    )
    with (
        DedupIndex(sandbox / "dedup-internal.sqlite3") as index,
        FrameworkState(_normal_state_database(sandbox)) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = _begin_normal_run(
            state,
            corpus,
            internal_paths_policy=internal_policy,
        )
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )

        with pytest.raises(
            InternalPathProtectionError,
            match="internal repository",
        ):
            actions._rename_mismatch(
                planned,
                detected,
                ActionSummary(apply_actions=True),
            )

        assert state._connection.execute(
            "SELECT COUNT(*) FROM file_actions WHERE run_id=?",
            (run_id,),
        ).fetchone() == (0,)

    assert native_calls == 0
    assert source.read_bytes() == b"protected-source"
    assert not destination.exists()


def test_rename_mismatch_signature_is_frozen() -> None:
    from inspect import signature

    assert str(signature(FrameworkActions._rename_mismatch)) == (
        "(self, planned, detected, summary: 'ActionSummary') -> 'ActionSummary'"
    )


# endregion [02]
