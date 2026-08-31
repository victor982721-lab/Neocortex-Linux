# region [00] Contexto del módulo
# Módulo: tests/test_document_organization_identity_bound.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from neocortex.deduplication import snapshot_path
from neocortex.documents import document_organization_application as application
from neocortex.documents import document_organization_planning as planning
from neocortex.api.cli.cli_parser import build_parser
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
    ProtectedAnalysisRootError,
)
from neocortex.documents.document_catalog import (
    document_catalog_database,
    initialize_document_catalog,
)
from neocortex.safety.internal_paths import (
    InternalPathProtectionError,
    InternalPathSpec,
    InternalPathsPolicy,
)
from neocortex.safety.protected_content import (
    ProtectedContentError,
    ProtectedContentPolicy,
    ProtectedPathSpec,
)
from neocortex.safety.windows_handle_mutation import (
    MutationEffectUncertainError,
)
from tests.internal_paths_test_support import disjoint_internal_paths_policy
from tests.mutation_containment import ContainedMutationRoot
# endregion [01]

# region [02] Implementación


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="identity-bound organization moves require Windows"
)


@pytest.fixture
def mutation_containment(tmp_path: Path) -> Iterator[ContainedMutationRoot]:
    base = tmp_path / "native-mutation-roots"
    base.mkdir()
    containment = ContainedMutationRoot.create(base, watch_directories=(base,))
    yield containment
    containment.assert_no_leaks()


def _normal_mutation_guard(root: Path) -> CorpusMutationGuard:
    return CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", root),
        disjoint_internal_paths_policy(root),
    )


def _analyze_only_mutation_guard(root: Path) -> CorpusMutationGuard:
    return CorpusMutationGuard(
        CorpusAccessPolicy.capture("analyze_only", root),
        disjoint_internal_paths_policy(root),
    )


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


def _internal_mutation_guard(
    root: Path,
    repository: Path,
    application_data: Path,
) -> CorpusMutationGuard:
    reservation = root.parent / f"{root.name}-guard-layout"
    runtime = reservation / "Programs" / "Neocortex"
    internal_policy = InternalPathsPolicy.capture(
        (
            InternalPathSpec("repository", "tree", repository),
            InternalPathSpec("runtime", "tree", runtime),
            InternalPathSpec("application_data", "tree", application_data),
            InternalPathSpec(
                "launcher",
                "file",
                runtime / "bin" / "Neocortex.exe",
            ),
        )
    )
    return CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", root),
        internal_policy,
    )


@pytest.mark.parametrize("blocked_side", ("source", "destination"))
def test_internal_path_fails_before_destination_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_side: str,
) -> None:
    repository = tmp_path / "internal-repository"
    application_data = tmp_path / "internal-application-data"
    repository.mkdir()
    application_data.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    root = tmp_path / "organized"
    root.mkdir()
    ordinary_source = tmp_path / "ordinary-source.bin"
    ordinary_source.write_bytes(b"ordinary")
    source = repository / "protected-source.bin" if blocked_side == "source" else ordinary_source
    if blocked_side == "source":
        source.write_bytes(b"protected")
    destination = (
        application_data / "protected-target.bin"
        if blocked_side == "destination"
        else root / "ordinary-target.bin"
    )
    guard = _internal_mutation_guard(tmp_path, repository, application_data)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT ? AS source_path, ? AS destination_path",
        (str(source), str(destination)),
    ).fetchone()
    assert row is not None
    recovery_calls = 0

    def unexpected_recovery(*_args: object) -> None:
        nonlocal recovery_calls
        recovery_calls += 1

    monkeypatch.setattr(
        application,
        "_recover_organization_destination",
        unexpected_recovery,
    )
    with pytest.raises(InternalPathProtectionError, match="internal_framework_root"):
        application._apply_one_plan(
            row,
            state_directory,
            root,
            os.stat(root, follow_symlinks=False),
            guard,
        )

    connection.close()
    assert recovery_calls == 0
    assert not destination.exists()


def test_missing_internal_organization_root_never_calls_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "internal-repository"
    application_data = tmp_path / "internal-application-data"
    repository.mkdir()
    application_data.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    root = repository / "organized"
    guard = _internal_mutation_guard(tmp_path, repository, application_data)
    mkdir_calls = 0

    def forbidden_mkdir(_path: Path, *_args: object, **_kwargs: object) -> None:
        nonlocal mkdir_calls
        mkdir_calls += 1
        raise AssertionError("internal organization root mkdir was reached")

    monkeypatch.setattr(Path, "mkdir", forbidden_mkdir)
    with pytest.raises(InternalPathProtectionError, match="normal corpus root"):
        application._prepare_apply_root(
            state_directory / "document_catalog.sqlite3",
            root,
            guard,
        )

    assert mkdir_calls == 0
    assert not root.exists()


def test_internal_identity_change_aborts_native_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "internal-repository"
    application_data = tmp_path / "internal-application-data"
    repository.mkdir()
    application_data.mkdir()
    guard = _internal_mutation_guard(tmp_path, repository, application_data)
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    expected = snapshot_path(source)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    root = tmp_path / "organized"
    root.mkdir()
    destination = root / "target.bin"
    displaced_repository = tmp_path / "internal-repository-old"
    callback_calls = 0
    native_calls = 0

    def controlled_rename(
        _source: Path,
        _destination: Path,
        _expected: object,
        *,
        before_native_call: object,
    ) -> object:
        nonlocal callback_calls, native_calls
        repository.rename(displaced_repository)
        repository.mkdir()
        assert callable(before_native_call)
        callback_calls += 1
        before_native_call()
        native_calls += 1
        raise AssertionError("native rename must remain unreachable")

    monkeypatch.setattr(
        application,
        "rename_no_replace_by_identity",
        controlled_rename,
    )
    with pytest.raises(
        InternalPathProtectionError,
        match="internal path identity changed",
    ):
        application._move_organization_source(
            source,
            destination,
            expected,
            state_directory,
            root,
            os.stat(root, follow_symlinks=False),
            guard,
        )

    assert callback_calls == 1
    assert native_calls == 0
    assert source.read_bytes() == b"source"
    assert not destination.exists()


def test_apply_rejects_analyze_only_before_any_other_validation(
    tmp_path: Path,
) -> None:
    protected_root = tmp_path / "protected"
    protected_root.mkdir()
    catalog = tmp_path / "missing-state" / "document_catalog.sqlite3"
    organization_root = tmp_path / "missing-organization"

    with pytest.raises(ProtectedAnalysisRootError, match="protected_analysis_root"):
        application.apply_document_organization(
            catalog,
            organization_root,
            mutation_guard=_analyze_only_mutation_guard(protected_root),
            max_actions=0,
        )

    assert not catalog.parent.exists()
    assert not organization_root.exists()


def test_apply_all_rejects_analyze_only_before_any_other_validation(
    tmp_path: Path,
) -> None:
    protected_root = tmp_path / "protected"
    protected_root.mkdir()
    catalog = tmp_path / "missing-state" / "document_catalog.sqlite3"
    organization_root = tmp_path / "missing-organization"

    with pytest.raises(ProtectedAnalysisRootError, match="protected_analysis_root"):
        application.apply_all_document_organization(
            catalog,
            organization_root,
            mutation_guard=_analyze_only_mutation_guard(protected_root),
            batch_size=0,
        )

    assert not catalog.parent.exists()
    assert not organization_root.exists()


def _install_contained_rename(
    monkeypatch: pytest.MonkeyPatch,
    containment: ContainedMutationRoot,
) -> None:
    original = application.rename_no_replace_by_identity

    def contained_rename(
        source: Path,
        destination: Path,
        expected: object,
        **kwargs: object,
    ) -> object:
        return containment.call_rename(
            original,
            source,
            destination,
            expected,
            **kwargs,
        )

    monkeypatch.setattr(application, "rename_no_replace_by_identity", contained_rename)


def test_organization_move_uses_identity_bound_no_replace(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = mutation_containment.root
    source = sandbox / "source.txt"
    source.write_text("authorized", encoding="utf-8")
    expected = snapshot_path(source)
    state_directory = sandbox / "state"
    state_directory.mkdir()
    root = sandbox / "organized"
    root.mkdir()
    root_stat = os.stat(root, follow_symlinks=False)
    destination = root / "nested" / "target.txt"
    destination.parent.mkdir()
    _install_contained_rename(monkeypatch, mutation_containment)

    status, detail = application._move_organization_source(
        source,
        destination,
        expected,
        state_directory,
        root,
        root_stat,
        _normal_mutation_guard(root),
    )

    assert status == "moved"
    assert "identity-bound" in detail
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "authorized"


def test_organization_move_abstains_for_hard_link(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = mutation_containment.root
    source = sandbox / "source.txt"
    source.write_text("authorized", encoding="utf-8")
    linked = sandbox / "linked.txt"
    os.link(source, linked)
    expected = snapshot_path(source)
    state_directory = sandbox / "state"
    state_directory.mkdir()
    root = sandbox / "organized"
    root.mkdir()
    root_stat = os.stat(root, follow_symlinks=False)
    destination = root / "target.txt"
    _install_contained_rename(monkeypatch, mutation_containment)

    status, detail = application._move_organization_source(
        source,
        destination,
        expected,
        state_directory,
        root,
        root_stat,
        _normal_mutation_guard(root),
    )

    assert status == "blocked"
    assert "hard links" in detail
    assert source.exists()
    assert linked.exists()
    assert not destination.exists()


def test_post_effect_failure_requires_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("authorized", encoding="utf-8")
    expected = snapshot_path(source)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    root = tmp_path / "organized"
    root.mkdir()
    root_stat = os.stat(root, follow_symlinks=False)
    destination = root / "target.txt"

    def uncertain(*_args: object, **_kwargs: object) -> None:
        raise MutationEffectUncertainError(source, destination, RuntimeError("fault"))

    monkeypatch.setattr(application, "rename_no_replace_by_identity", uncertain)
    status, detail = application._move_organization_source(
        source,
        destination,
        expected,
        state_directory,
        root,
        root_stat,
        _normal_mutation_guard(root),
    )

    assert status == "recovery_required"
    assert "confirmation failed" in detail


def test_state_source_is_blocked_before_destination_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = state_directory / "framework-owned.bin"
    source.write_bytes(b"state")
    root = tmp_path / "organized"
    root.mkdir()
    destination = root / "nested" / "target.bin"
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT ? AS source_path, ? AS destination_path",
        (str(source), str(destination)),
    ).fetchone()
    assert row is not None
    recovery_calls = 0

    def unexpected_recovery(*_args: object) -> None:
        nonlocal recovery_calls
        recovery_calls += 1

    monkeypatch.setattr(
        application,
        "_recover_organization_destination",
        unexpected_recovery,
    )

    status, detail = application._apply_one_plan(
        row,
        state_directory,
        root,
        os.stat(root, follow_symlinks=False),
        _normal_mutation_guard(root),
    )

    connection.close()
    assert status == "blocked"
    assert "organization source and framework state directory" in detail
    assert recovery_calls == 0
    assert not destination.parent.exists()


def test_selected_state_source_is_rejected_before_initial_root_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    catalog = state_directory / "document_catalog.sqlite3"
    source = state_directory / "framework-owned.bin"
    source.write_bytes(b"state")
    root = tmp_path / "organized"
    destination = root / "target.bin"
    initialize_document_catalog(catalog)
    with document_catalog_database(catalog) as connection:
        _insert_plan(
            connection,
            plan_id=1,
            status="planned",
            destination=str(destination),
        )
        connection.execute(
            """UPDATE organization_plans
            SET source_path=?,organization_root=? WHERE plan_id=1""",
            (str(source), str(root)),
        )
        connection.commit()
    recovery_calls = 0

    def unexpected_recovery(*_args: object) -> None:
        nonlocal recovery_calls
        recovery_calls += 1

    monkeypatch.setattr(
        application,
        "_recover_organization_destination",
        unexpected_recovery,
    )

    with pytest.raises(
        ValueError,
        match="organization source and framework state directory",
    ):
        application.apply_document_organization(
            catalog,
            root,
            mutation_guard=_normal_mutation_guard(tmp_path),
            max_actions=1,
        )

    assert recovery_calls == 0
    assert not root.exists()
    assert source.read_bytes() == b"state"


def test_root_swap_is_rejected_before_destination_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    root = tmp_path / "organized"
    root.mkdir()
    root_stat = os.stat(root, follow_symlinks=False)
    displaced_root = tmp_path / "organized-before-swap"
    destination = root / "nested" / "target.bin"
    original_intersection = application.path_trees_intersect
    swapped = False

    def swap_root_after_boundary_read(left: Path, right: Path) -> bool:
        nonlocal swapped
        result = original_intersection(left, right)
        if not swapped:
            root.rename(displaced_root)
            root.mkdir()
            swapped = True
        return result

    monkeypatch.setattr(
        application,
        "path_trees_intersect",
        swap_root_after_boundary_read,
    )

    with pytest.raises(ValueError, match="organization root identity changed"):
        application._create_destination_parent(
            state_directory,
            source,
            root,
            destination,
            root_stat,
            _normal_mutation_guard(root),
        )

    assert swapped
    assert not (root / "nested").exists()
    assert not (displaced_root / "nested").exists()


def test_move_revalidates_root_in_before_native_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    expected = snapshot_path(source)
    root = tmp_path / "organized"
    root.mkdir()
    root_stat = os.stat(root, follow_symlinks=False)
    displaced_root = tmp_path / "organized-before-native-call"
    destination = root / "target.bin"
    callback_calls = 0
    native_calls = 0

    def controlled_rename(
        _source: Path,
        _destination: Path,
        _expected: object,
        *,
        before_native_call: object,
    ) -> object:
        nonlocal callback_calls, native_calls
        root.rename(displaced_root)
        root.mkdir()
        assert callable(before_native_call)
        callback_calls += 1
        before_native_call()
        native_calls += 1
        raise AssertionError("native rename must remain unreachable")

    monkeypatch.setattr(
        application,
        "rename_no_replace_by_identity",
        controlled_rename,
    )

    status, detail = application._move_organization_source(
        source,
        destination,
        expected,
        state_directory,
        root,
        root_stat,
        _normal_mutation_guard(root),
    )

    assert status == "blocked"
    assert "organization root identity changed" in detail
    assert callback_calls == 1
    assert native_calls == 0
    assert source.read_bytes() == b"source"
    assert not destination.exists()


def test_protected_analysis_error_is_not_degraded_to_operational_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    expected = snapshot_path(source)
    root = tmp_path / "organized"
    root.mkdir()
    destination = root / "target.bin"

    def reject_protected(*_args: object, **_kwargs: object) -> None:
        raise ProtectedAnalysisRootError("organization test guard")

    monkeypatch.setattr(
        application,
        "rename_no_replace_by_identity",
        reject_protected,
    )

    with pytest.raises(ProtectedAnalysisRootError, match="protected_analysis_root"):
        application._move_organization_source(
            source,
            destination,
            expected,
            state_directory,
            root,
            os.stat(root, follow_symlinks=False),
            _normal_mutation_guard(root),
        )


def test_recovery_required_plan_remains_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    initialize_document_catalog(catalog)
    with document_catalog_database(catalog) as connection:
        _insert_plan(connection, plan_id=1, status="applying", destination="target")
        row = connection.execute("SELECT * FROM organization_plans WHERE plan_id=1").fetchone()
        assert row is not None
        monkeypatch.setattr(
            application,
            "_disambiguate_apply_destination",
            lambda _connection, selected, _mutation_guard: selected,
        )
        monkeypatch.setattr(application, "_catalog_destination_conflict", lambda *_args: False)
        monkeypatch.setattr(
            application,
            "_apply_one_plan",
            lambda *_args: ("recovery_required", "effect is uncertain"),
        )

        outcome = application._apply_selected_organization_plan(
            connection,
            catalog,
            row,
            tmp_path,
            os.stat(tmp_path, follow_symlinks=False),
            _normal_mutation_guard(tmp_path),
        )
        stored = connection.execute(
            "SELECT status,completed_ns FROM organization_plans WHERE plan_id=1"
        ).fetchone()

    assert outcome.status == "recovery_required"
    assert tuple(stored) == ("recovery_required", None)


def test_recovery_required_destination_remains_reserved(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    initialize_document_catalog(catalog)
    with document_catalog_database(catalog) as connection:
        _insert_plan(
            connection,
            plan_id=1,
            status="recovery_required",
            destination="same-target",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_plan(
                connection,
                plan_id=2,
                status="planned",
                destination="same-target",
            )


def test_apply_disambiguation_rejects_protected_alternative_before_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    occupied = tmp_path / "occupied.bin"
    occupied.write_bytes(b"occupied")
    protected_root = tmp_path / "read-only"
    protected_root.mkdir()
    protected_alternative = protected_root / "alternate.bin"
    guard = _protected_mutation_guard(tmp_path, protected_root)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """CREATE TABLE organization_plans(
            plan_id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL,
            destination_path TEXT,
            reason TEXT NOT NULL,
            detail TEXT,
            status TEXT NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO organization_plans VALUES(1,?,?,?,?,?)",
            (
                str(source),
                str(occupied),
                "classification_above_threshold",
                None,
                "planned",
            ),
        )
        row = connection.execute("SELECT * FROM organization_plans WHERE plan_id=1").fetchone()
        assert row is not None
        monkeypatch.setattr(
            application,
            "_resolve_plan_destination",
            lambda *_args: (protected_alternative, True),
        )

        with pytest.raises(ProtectedContentError, match="protected_content_root"):
            application._disambiguate_apply_destination(
                connection,
                row,
                guard,
            )

        stored = connection.execute(
            "SELECT destination_path,reason,detail FROM organization_plans WHERE plan_id=1"
        ).fetchone()
        assert stored is not None
        assert tuple(stored) == (
            str(occupied),
            "classification_above_threshold",
            None,
        )
    finally:
        connection.close()

    assert not protected_alternative.exists()


def test_planner_does_not_reuse_recovery_required_destination(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    destination = tmp_path / "reserved.bin"
    initialize_document_catalog(catalog)
    with document_catalog_database(catalog) as connection:
        _insert_plan(
            connection,
            plan_id=1,
            status="recovery_required",
            destination=str(destination),
        )
        candidate = connection.execute(
            "SELECT 'pdf' AS source_kind,'other-file' AS file_key"
        ).fetchone()
        assert candidate is not None

        assert not planning._plan_destination_available(
            connection,
            candidate,
            destination,
        )


def test_cli_can_filter_recovery_required_organization_plans() -> None:
    args = build_parser().parse_args(
        (
            "--organization-preview",
            "1",
            "--organization-preview-status",
            "recovery_required",
        )
    )

    assert args.organization_preview_status == "recovery_required"


def _insert_plan(
    connection: sqlite3.Connection,
    *,
    plan_id: int,
    status: str,
    destination: str,
) -> None:
    connection.execute(
        """INSERT INTO organization_plans(
        plan_id,source_kind,file_key,source_path,destination_path,organization_root,
        volume_id,file_id,size,mtime_ns,birthtime_ns,classifier_signature,
        primary_kind,confidence,status,reason,evidence_json,planned_ns,
        cache_sync_status)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            plan_id,
            "pdf",
            f"file-{plan_id}",
            f"source-{plan_id}",
            destination,
            "root",
            "1",
            str(plan_id),
            1,
            2,
            3,
            "classifier",
            "document",
            1.0,
            status,
            "test",
            "{}",
            4,
            "not_required",
        ),
    )


# endregion [02]
