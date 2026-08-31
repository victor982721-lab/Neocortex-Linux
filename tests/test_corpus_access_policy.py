"""Durable fail-closed contracts for analyze-only corpus roots."""
# region [00] Contexto del módulo
# Módulo: tests/test_corpus_access_policy.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import neocortex.safety.corpus_access as corpus_access_module
from neocortex.enumeration import JournalCursor
from neocortex.deduplication import InventoryExclusionPolicy
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
    ProtectedAnalysisRootError,
    path_trees_intersect,
)
from neocortex.safety.internal_paths import InternalPathProtectionError
from neocortex.integrations.inventory.inventory_boundary import (
    build_normal_inventory_boundary,
)
from neocortex.safety.protected_content import (
    ProtectedContentError,
    ProtectedContentPolicy,
    ProtectedPathSpec,
)
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import disjoint_internal_paths_policy
# endregion [01]

# region [02] Implementación


def _fixture_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "corpus"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    return root, state_directory, state_directory / "framework.sqlite3"


def _extended_drive_alias(path: Path) -> Path:
    return Path("\\\\?\\" + os.path.abspath(path))




def test_analyze_only_guard_distinguishes_root_descendants_and_siblings(
    tmp_path: Path,
) -> None:
    root, _state_directory, _database = _fixture_paths(tmp_path)
    child = root / "pkg" / "module.py"
    child.parent.mkdir()
    child.write_text("value = 1\n", encoding="utf-8")
    sibling = root.parent / f"{root.name}2" / "module.py"
    sibling.parent.mkdir()
    sibling.write_text("value = 2\n", encoding="utf-8")
    policy = CorpusAccessPolicy.capture("analyze_only", root)
    internal_policy = disjoint_internal_paths_policy(tmp_path)
    guard = CorpusMutationGuard(policy, internal_policy)

    protected_paths = (
        root.parent,
        root,
        child,
        root / "pkg" / ".." / "pkg" / "new.py",
        root / "missing" / "target.py",
    )
    if os.name == "nt":
        protected_paths = (*protected_paths, Path(str(child).swapcase()))
    for protected in protected_paths:
        with pytest.raises(
            ProtectedAnalysisRootError,
            match="protected_analysis_root",
        ):
            guard.require_paths_allowed(protected)

    guard.require_paths_allowed(sibling)
    with pytest.raises(ProtectedAnalysisRootError):
        guard.require_paths_allowed(sibling, child)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-path contract")
def test_extended_drive_alias_preserves_identity_and_is_guarded(
    tmp_path: Path,
) -> None:
    root, _state_directory, _database = _fixture_paths(tmp_path)
    alias = _extended_drive_alias(root)
    root_metadata = os.stat(root)
    alias_metadata = os.stat(alias)

    assert path_trees_intersect(root, alias)
    assert (alias_metadata.st_dev, alias_metadata.st_ino) == (
        root_metadata.st_dev,
        root_metadata.st_ino,
    )
    with pytest.raises(ProtectedAnalysisRootError):
        CorpusMutationGuard(
            CorpusAccessPolicy.capture("analyze_only", root),
            disjoint_internal_paths_policy(tmp_path),
        ).require_paths_allowed(alias)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-path contract")
def test_extended_unc_alias_is_lexically_equivalent() -> None:
    assert path_trees_intersect(
        r"\\server\share\corpus",
        r"\\?\UNC\server\share\corpus\child",
    )


@pytest.mark.parametrize(
    "candidate",
    (
        r"\\.\PhysicalDrive0",
        r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1",
        r"\??\C:\Users\Neocortex",
    ),
)
@pytest.mark.skipif(os.name != "nt", reason="Windows namespace contract")
def test_non_equivalent_windows_namespace_fails_before_physical_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
) -> None:
    root, _state_directory, _database = _fixture_paths(tmp_path)
    policy = CorpusAccessPolicy.capture("analyze_only", root)
    internal_policy = disjoint_internal_paths_policy(tmp_path)
    original_lstat = os.lstat
    test_parent_key = os.path.normcase(os.path.abspath(tmp_path.parent))

    def guarded_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        path_key = os.path.normcase(os.path.abspath(path))
        try:
            within_test_parent = os.path.commonpath((path_key, test_parent_key)) == test_parent_key
        except ValueError:
            within_test_parent = False
        if not within_test_parent:
            raise AssertionError("unsupported namespace reached physical inspection")
        return original_lstat(path)

    monkeypatch.setattr(corpus_access_module.os, "lstat", guarded_lstat)
    with pytest.raises(ValueError, match="unsupported Windows namespace"):
        path_trees_intersect(root, candidate)
    with pytest.raises(
        InternalPathProtectionError,
        match="internal mutation boundary cannot be verified",
    ):
        CorpusMutationGuard(policy, internal_policy).require_paths_allowed(candidate)


def test_capture_rejects_reparse_semantics_through_test_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _state_directory, _database = _fixture_paths(tmp_path)
    monkeypatch.setattr(
        corpus_access_module,
        "_has_reparse_semantics",
        lambda _path, _metadata: True,
    )

    with pytest.raises(ValueError, match="symlink or reparse point"):
        CorpusAccessPolicy.capture("analyze_only", root)


def test_changed_root_identity_fails_closed_with_stable_reason(tmp_path: Path) -> None:
    root, _state_directory, _database = _fixture_paths(tmp_path)
    policy = CorpusAccessPolicy.capture("analyze_only", root)
    internal_policy = disjoint_internal_paths_policy(tmp_path)
    changed = replace(policy, root_file_id=policy.root_file_id + 1)  # type: ignore[operator]

    guard = CorpusMutationGuard(changed, internal_policy)
    with pytest.raises(ProtectedAnalysisRootError) as raised:
        guard.reject_run_mutation()

    assert raised.value.reason_code == "protected_analysis_root"
    with pytest.raises(ProtectedAnalysisRootError):
        guard.require_paths_allowed(tmp_path / "outside")


def test_normal_guard_enforces_protected_content_paths_and_read_only_run(
    tmp_path: Path,
) -> None:
    ordinary = tmp_path / "ordinary"
    protected = tmp_path / "protected"
    ordinary.mkdir()
    protected.mkdir()
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "read-only",
                "tree",
                "analyze_read_only",
                protected,
            ),
        )
    )
    internal_policy = disjoint_internal_paths_policy(tmp_path)
    read_only_guard = CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", protected),
        internal_policy,
        protected_policy,
    )

    assert read_only_guard.reason_code == "protected_content_root"
    with pytest.raises(ProtectedContentError, match="protected_content_root"):
        read_only_guard.reject_run_mutation()

    ordinary_guard = CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", ordinary),
        internal_policy,
        protected_policy,
    )
    ordinary_guard.require_paths_allowed(ordinary / "allowed.bin")
    for blocked in (tmp_path, protected, protected / "child.bin"):
        with pytest.raises(ProtectedContentError, match="protected_content_root"):
            ordinary_guard.require_paths_allowed(blocked)


def test_normal_guard_classifies_a_path_batch_with_one_policy_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary = tmp_path / "ordinary"
    protected = tmp_path / "protected"
    ordinary.mkdir()
    protected.mkdir()
    policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "read-only",
                "tree",
                "analyze_read_only",
                protected,
            ),
        )
    )
    guard = CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", ordinary),
        disjoint_internal_paths_policy(tmp_path),
        policy,
    )
    identity_revalidations = 0
    original_verify = ProtectedContentPolicy.verify_identities

    def counted_verify(current: ProtectedContentPolicy) -> None:
        nonlocal identity_revalidations
        identity_revalidations += 1
        original_verify(current)

    monkeypatch.setattr(ProtectedContentPolicy, "verify_identities", counted_verify)

    reasons = guard.mutation_path_protection_reasons(
        ordinary / "first.bin",
        protected / "blocked.bin",
        ordinary / "second.bin",
    )

    assert reasons[0] is None
    assert reasons[1] is not None and "read-only" in reasons[1]
    assert reasons[2] is None
    assert identity_revalidations == 2












def test_normal_actions_remain_compatible_and_policy_columns_are_immutable(
    tmp_path: Path,
) -> None:
    root, _state_directory, database = _fixture_paths(tmp_path)
    with FrameworkState(database) as state:
        boundary = build_normal_inventory_boundary(root, database.parent)
        run_id = state.begin_initial_run(
            root,
            JournalCursor("C:", 1, 10),
            inventory_policy_signature=boundary.effective_signature,
        )
        action_id = state.begin_file_action(
            run_id,
            "fixture",
            str(root / "source.py"),
            None,
            None,
            None,
            True,
        )
        assert state._connection.execute(
            """SELECT corpus_access_mode,protected_root,
            protected_root_device_id_hex,protected_root_file_id_hex,
            protected_root_birthtime_ns FROM file_actions WHERE action_id=?""",
            (action_id,),
        ).fetchone() == ("normal", None, None, None, None)
        state.mark_file_actions_applying(((action_id, "{}"),))
        with pytest.raises(sqlite3.IntegrityError, match="corpus policy is immutable"):
            state._connection.execute(
                "UPDATE file_actions SET corpus_access_mode='analyze_only' WHERE action_id=?",
                (action_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="corpus policy is immutable"):
            state._connection.execute(
                "UPDATE initial_runs SET corpus_access_mode='analyze_only' WHERE run_id=?",
                (run_id,),
            )


@pytest.mark.parametrize("physical_target", ("root", "ancestor"))
def test_physical_intersection_with_protected_root_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    physical_target: str,
) -> None:
    root, _state_directory, _database = _fixture_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    policy = CorpusAccessPolicy.capture("analyze_only", root)
    internal_policy = disjoint_internal_paths_policy(tmp_path)
    realpath = os.path.realpath
    redirected_target = root if physical_target == "root" else root.parent
    candidate = outside / "new.py" if physical_target == "root" else outside

    def redirected(path: str | os.PathLike[str]) -> str:
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(outside)):
            return str(redirected_target)
        return realpath(path)

    monkeypatch.setattr(corpus_access_module.os.path, "realpath", redirected)
    with pytest.raises(ProtectedAnalysisRootError):
        CorpusMutationGuard(
            policy,
            internal_policy,
        ).require_paths_allowed(candidate)


def test_physical_boundary_inspection_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _state_directory, _database = _fixture_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    policy = CorpusAccessPolicy.capture("analyze_only", root)
    internal_policy = disjoint_internal_paths_policy(tmp_path)
    original_lstat = os.lstat
    outside_key = os.path.normcase(os.path.abspath(outside))

    def inaccessible(path: str | os.PathLike[str]) -> os.stat_result:
        if os.path.normcase(os.path.abspath(path)) == outside_key:
            raise PermissionError("simulated inaccessible boundary")
        return original_lstat(path)

    monkeypatch.setattr(corpus_access_module.os, "lstat", inaccessible)
    with pytest.raises(
        InternalPathProtectionError,
        match="internal mutation boundary cannot be verified",
    ):
        CorpusMutationGuard(policy, internal_policy).require_paths_allowed(outside / "new.py")


# endregion [02]
