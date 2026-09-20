# region [00] Contexto del módulo
# Módulo: tests/test_normal_inventory_boundary.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from neocortex.foundation.hash_compat import HASH_ALGORITHM_128

from neocortex.deduplication import DedupIndex
from neocortex.integrations.inventory import inventory_boundary as inventory_boundary_module
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    ProtectedAnalysisRootError,
)
from neocortex.safety.internal_paths import (
    InternalPathSpec,
    InternalPathsPolicy,
)
from neocortex.runtime.orchestration.orchestrator import (
    build_normal_inventory_boundary,
    initialize_authorized_state_directory,
)
from neocortex.safety.protected_content import (
    ProtectedContentError,
    ProtectedContentPolicy,
    ProtectedPathSpec,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import (
    begin_signed_normal_run,
    disjoint_internal_paths_policy,
)
# endregion [01]

# region [02] Implementación


def _internal_policy_factory(base: Path):
    repository = base / "Repository"
    runtime = base / "Programs" / "Neocortex"
    application_data = base / "Local" / "Neocortex"
    launcher = runtime / "bin" / "Neocortex.exe"
    specs = (
        InternalPathSpec("repository", "tree", repository),
        InternalPathSpec("runtime", "tree", runtime),
        InternalPathSpec("application_data", "tree", application_data),
        InternalPathSpec("launcher", "file", launcher),
    )

    def capture() -> InternalPathsPolicy:
        return InternalPathsPolicy.capture(specs)

    return capture, repository, runtime, application_data, launcher


@pytest.fixture(autouse=True)
def _empty_default_protected_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ProtectedContentPolicy.capture(())
    monkeypatch.setattr(
        inventory_boundary_module,
        "canonical_protected_content_policy",
        lambda: policy,
    )
    monkeypatch.setattr(
        "neocortex.persistence.framework_state_common.canonical_protected_content_policy",
        lambda: policy,
    )


def test_fresh_state_materialization_is_fenced_and_policy_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture, repository, runtime, application_data, launcher = _internal_policy_factory(
        tmp_path / "internal"
    )
    repository.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    monkeypatch.setattr(
        inventory_boundary_module,
        "canonical_internal_paths_policy",
        capture,
    )
    corpus = tmp_path
    access = CorpusAccessPolicy.capture("normal", corpus)
    state = application_data / "state"

    layout = initialize_authorized_state_directory(
        access,
        state,
        require_disjoint=False,
    )
    boundary = build_normal_inventory_boundary(
        corpus,
        layout.path,
        access_policy=access,
        state_policy=layout.state_policy,
        internal_paths_policy=layout.internal_paths_policy,
    )

    assert layout.path == state
    assert state.is_dir()
    assert runtime.is_dir()
    assert boundary.internal_paths_policy.signature == layout.internal_paths_policy.signature
    boundary.verify()


def test_state_setup_never_creates_inside_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture, repository, _, application_data, launcher = _internal_policy_factory(
        tmp_path / "internal"
    )
    repository.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    application_data.mkdir(parents=True)
    monkeypatch.setattr(
        inventory_boundary_module,
        "canonical_internal_paths_policy",
        capture,
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    access = CorpusAccessPolicy.capture("normal", corpus)
    forbidden_state = repository / "state"

    with pytest.raises(ValueError, match="protected code/runtime"):
        initialize_authorized_state_directory(
            access,
            forbidden_state,
            require_disjoint=False,
        )

    assert not forbidden_state.exists()


def test_boundary_rejects_replaced_state_child(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    boundary = build_normal_inventory_boundary(root, state)

    state.rename(tmp_path / "original-state")
    state.mkdir()

    with pytest.raises(ProtectedAnalysisRootError, match="root identity changed"):
        boundary.verify()


@pytest.mark.parametrize("relation", ("same", "parent", "ancestor"))
def test_boundary_rejects_state_that_contains_corpus(
    tmp_path: Path,
    relation: str,
) -> None:
    if relation == "same":
        root = tmp_path / "corpus"
        state = root
    elif relation == "parent":
        state = tmp_path
        root = state / "corpus"
    else:
        state = tmp_path / "state-owner"
        root = state / "nested" / "corpus"
    root.mkdir(parents=True)

    with pytest.raises(ValueError, match="cannot equal or contain"):
        build_normal_inventory_boundary(root, state)


def test_boundary_allows_and_excludes_state_below_corpus(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state = root / "state"
    state.mkdir(parents=True)

    boundary = build_normal_inventory_boundary(root, state)

    assert boundary.exclusion_policy.excludes_directory(state)
    boundary.verify()


def test_boundary_compiles_restricted_codex_allowlist_and_v2_signature(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    codex = profile / ".codex"
    sessions = codex / "sessions"
    sessions.mkdir(parents=True)
    agents = codex / "AGENTS.md"
    agents.write_text("safe instructions", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec("codex", "tree", "exclude", codex),
            ProtectedPathSpec(
                "sessions",
                "tree",
                "analyze_read_only",
                sessions,
            ),
            ProtectedPathSpec(
                "agents",
                "file",
                "analyze_read_only",
                agents,
            ),
        )
    )

    boundary = build_normal_inventory_boundary(
        profile,
        state,
        internal_paths_policy=disjoint_internal_paths_policy(tmp_path),
        protected_content_policy=protected_policy,
    )

    assert boundary.protected_content_policy == protected_policy
    assert boundary.effective_signature.startswith(
        f"effective-inventory-policy-v2:{HASH_ALGORITHM_128.replace('-', '_')}:"
    )
    assert not boundary.exclusion_policy.excludes_directory(codex)
    assert not boundary.exclusion_policy.excludes_directory(sessions)
    assert not boundary.exclusion_policy.excludes_file(agents)
    assert boundary.exclusion_policy.excludes_directory(codex / "unlisted")
    assert boundary.exclusion_policy.excludes_directory(sessions / "cache")
    assert boundary.exclusion_policy.excludes_file(sessions / "auth.json")
    assert not boundary.exclusion_policy.excludes_file(sessions / "visible.jsonl")
    assert boundary.exclusion_policy.excludes_directory(
        profile / "project" / ".venv",
        file_attributes=0,
    )
    assert boundary.exclusion_policy.excludes_directory(
        profile / "project" / "site-packages",
        file_attributes=0,
    )
    assert boundary.exclusion_policy.excludes_directory(
        profile / "project" / "node_modules",
        file_attributes=0,
    )
    assert boundary.exclusion_policy.excludes_directory(
        profile / "project" / "tmp1mfujc__",
        file_attributes=0,
    )
    assert boundary.exclusion_policy.excludes_directory(
        profile / "project" / "title-review-pytest",
        file_attributes=0,
    )
    assert boundary.exclusion_policy.excludes_file(profile / "project" / "cached.pyc")
    assert not boundary.exclusion_policy.excludes_directory(
        profile / "project" / "build",
        file_attributes=0,
    )
    boundary.verify()


def test_normal_inventory_omits_generated_trees_but_keeps_ambiguous_names(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    expected = {
        "visible.txt",
        "build/artifact.txt",
        "dist/deliverable.txt",
    }
    for relative_path in (
        *expected,
        ".venv/dependency.py",
        "project/site-packages/dependency.py",
        "web/node_modules/dependency.js",
        "source/__pycache__/cached.pyc",
        "source/orphan.pyc",
        ".CDX/reconstruction.xml",
        "tests/tmp1mfujc__/cache.bin",
        "tests/title-review-pytest/cache.bin",
        "tests/inline-snapshot-abcd/cache.bin",
    ):
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture", encoding="utf-8")

    boundary = build_normal_inventory_boundary(root, state)
    with DedupIndex(state / "dedup.sqlite3") as index:
        scan = index.scan(root, exclusion_policy=boundary.exclusion_policy)
        observed = {
            Path(snapshot.path).relative_to(root).as_posix()
            for snapshot in index.snapshots(scan.scan_id)
        }

    assert observed == expected


def test_state_setup_rejects_excluded_corpus_before_mkdir(tmp_path: Path) -> None:
    root = tmp_path / "protected-corpus"
    root.mkdir()
    state = tmp_path / "state-not-created"
    access = CorpusAccessPolicy.capture("normal", root)
    protected_policy = ProtectedContentPolicy.capture(
        (ProtectedPathSpec("blocked", "tree", "exclude", root),)
    )

    with pytest.raises(ProtectedContentError, match="protected_content_root"):
        initialize_authorized_state_directory(
            access,
            state,
            require_disjoint=False,
            protected_content_policy=protected_policy,
        )

    assert not state.exists()


def test_state_setup_rejects_protected_state_before_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = tmp_path / "corpus"
    protected_root = tmp_path / "protected"
    corpus.mkdir()
    protected_root.mkdir()
    state = protected_root / "missing-state"
    access = CorpusAccessPolicy.capture("normal", corpus)
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "protected-state",
                "tree",
                "exclude",
                protected_root,
            ),
        )
    )
    internal_policy = disjoint_internal_paths_policy(tmp_path / "internal")
    monkeypatch.setattr(
        inventory_boundary_module,
        "canonical_internal_paths_policy",
        lambda: internal_policy,
    )
    original_mkdir = Path.mkdir
    state_mkdir_calls = 0

    def observed_mkdir(path: Path, *args, **kwargs) -> None:
        nonlocal state_mkdir_calls
        if path == state:
            state_mkdir_calls += 1
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", observed_mkdir)

    with pytest.raises(ProtectedContentError, match="protected content"):
        initialize_authorized_state_directory(
            access,
            state,
            require_disjoint=False,
            protected_content_policy=protected_policy,
        )

    assert state_mkdir_calls == 0
    assert not state.exists()


def test_existing_protected_state_is_rejected_by_build_and_verify(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    state = tmp_path / "protected-state"
    corpus.mkdir()
    state.mkdir()
    internal_policy = disjoint_internal_paths_policy(tmp_path / "internal")
    unprotected_policy = ProtectedContentPolicy.capture(())
    boundary = build_normal_inventory_boundary(
        corpus,
        state,
        internal_paths_policy=internal_policy,
        protected_content_policy=unprotected_policy,
    )
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "protected-state",
                "tree",
                "exclude",
                state,
            ),
        )
    )

    with pytest.raises(ProtectedContentError, match="protected content"):
        build_normal_inventory_boundary(
            corpus,
            state,
            internal_paths_policy=internal_policy,
            protected_content_policy=protected_policy,
        )
    with pytest.raises(ProtectedContentError, match="protected content"):
        replace(
            boundary,
            protected_content_policy=protected_policy,
        ).verify()


def test_boundary_rejects_protected_framework_database_hardlink(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    state = tmp_path / "state"
    protected_file = tmp_path / "protected.sqlite3"
    target = state / "framework.sqlite3"
    corpus.mkdir()
    state.mkdir()
    protected_file.write_bytes(b"protected-database-bytes")
    try:
        os.link(protected_file, target)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "protected-framework-database",
                "file",
                "exclude",
                protected_file,
            ),
        )
    )
    before = protected_file.read_bytes()

    with pytest.raises(
        ProtectedContentError,
        match="protected-framework-database",
    ):
        build_normal_inventory_boundary(
            corpus,
            state,
            internal_paths_policy=disjoint_internal_paths_policy(tmp_path / "internal"),
            protected_content_policy=protected_policy,
        )

    assert protected_file.read_bytes() == before


def test_canonical_internal_state_remains_allowed_below_protected_appdata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture, repository, _, application_data, launcher = _internal_policy_factory(
        tmp_path / "internal"
    )
    repository.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    application_data.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        inventory_boundary_module,
        "canonical_internal_paths_policy",
        capture,
    )
    corpus = tmp_path / "external-corpus"
    corpus.mkdir()
    access = CorpusAccessPolicy.capture("normal", corpus)
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "appdata-container",
                "tree",
                "exclude",
                application_data.parent,
            ),
        )
    )

    layout = initialize_authorized_state_directory(
        access,
        application_data,
        require_disjoint=False,
        protected_content_policy=protected_policy,
    )
    boundary = build_normal_inventory_boundary(
        corpus,
        layout.path,
        access_policy=access,
        state_policy=layout.state_policy,
        internal_paths_policy=layout.internal_paths_policy,
        protected_content_policy=protected_policy,
    )

    assert layout.path == application_data
    assert application_data.is_dir()
    boundary.verify()


def test_protected_child_inside_authorized_appdata_is_still_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture, repository, _, application_data, launcher = _internal_policy_factory(
        tmp_path / "internal"
    )
    repository.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    protected_child = application_data / "protected-child"
    protected_child.mkdir(parents=True)
    state = protected_child / "state"
    monkeypatch.setattr(
        inventory_boundary_module,
        "canonical_internal_paths_policy",
        capture,
    )
    corpus = tmp_path / "external-corpus"
    corpus.mkdir()
    access = CorpusAccessPolicy.capture("normal", corpus)
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "specific-protected-child",
                "tree",
                "exclude",
                protected_child,
            ),
        )
    )

    with pytest.raises(ProtectedContentError, match="specific-protected-child"):
        initialize_authorized_state_directory(
            access,
            state,
            require_disjoint=False,
            protected_content_policy=protected_policy,
        )

    assert not state.exists()


def test_signed_normal_run_helper_persists_exact_effective_signature(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "fixture"
    root = sandbox / "corpus"
    state_directory = sandbox / "state"
    root.mkdir(parents=True)
    state_directory.mkdir()
    internal_paths_policy = disjoint_internal_paths_policy(sandbox)

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id = begin_signed_normal_run(
            state,
            root,
            internal_paths_policy=internal_paths_policy,
        )
        expected = build_normal_inventory_boundary(
            root,
            state_directory,
            internal_paths_policy=internal_paths_policy,
        )

        assert state.source_inventory_policy_signature(run_id) == (expected.effective_signature)

# endregion [02]
