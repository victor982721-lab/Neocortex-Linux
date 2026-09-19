"""Pre-admission inventory boundary regressions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from neocortex.deduplication.inventory.traversal import (
    FileObservation,
    InventoryTraversal,
    RootIdentity,
)
from neocortex.integrations.inventory import inventory_boundary as boundary_module
from neocortex.runtime.config.app_paths import default_state_directory
from neocortex.safety.corpus_access import CorpusAccessPolicy
from neocortex.safety.internal_paths import canonical_internal_paths_policy
from neocortex.safety.protected_content import (
    ProtectedContentPolicy,
    ProtectedPathSpec,
)


@dataclass(slots=True)
class _ObservationSink:
    rows: list[FileObservation] = field(default_factory=list)

    @property
    def full(self) -> bool:
        return False

    def append(self, observation: FileObservation) -> None:
        self.rows.append(observation)

    def flush(self) -> None:
        return None


def _write(path: Path, text: str = "fixture") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    config_home = home / "xdg-config"
    state_home = home / "xdg-state"
    data_home = home / "xdg-data"
    cache_home = home / "xdg-cache"
    private_tmp = home / "tmp"
    for path in (config_home, state_home, data_home, cache_home, private_tmp):
        path.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("TMPDIR", str(private_tmp))

    root = home / "Documents" / "Corpus"
    root.mkdir(parents=True)
    state = default_state_directory()
    state.mkdir(parents=True)
    runtime = data_home / "Neocortex"
    runtime.mkdir(parents=True)
    models = runtime / "models"
    models.mkdir()
    source = home / "Neocortex" / "Repository"
    source.mkdir(parents=True)
    codex = home / ".codex"
    codex.mkdir()
    protected = root / "protected-content"
    protected.mkdir()

    # The actual production constant is captured at module import.  Extend it
    # only inside this private fixture so canonical private-home roots remain
    # tested without consulting Victor's home or state.
    monkeypatch.setattr(
        boundary_module,
        "DEFAULT_EXCLUDED_PATHS",
        (
            *boundary_module.DEFAULT_EXCLUDED_PATHS,
            codex,
            state,
            runtime,
            source,
            config_home / "Neocortex",
        ),
    )
    internal_paths = canonical_internal_paths_policy()
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec("codex_home", "tree", "exclude", codex),
            ProtectedPathSpec("fixture-protected", "tree", "exclude", protected),
        )
    )
    access_policy = CorpusAccessPolicy.capture("normal", root)
    return root, state, runtime, models, source, codex, internal_paths, protected_policy, access_policy


def _boundary(fixture, *, observe: bool):
    (
        root,
        state,
        _runtime,
        _models,
        _source,
        _codex,
        internal_paths,
        protected_policy,
        access_policy,
    ) = fixture
    return boundary_module.build_normal_inventory_boundary(
        root,
        state,
        access_policy=access_policy,
        internal_paths_policy=internal_paths,
        protected_content_policy=protected_policy,
        observe_regenerable_artifacts=observe,
    )


def _observed_paths(root: Path, policy) -> set[str]:
    sink = _ObservationSink()
    traversal = InventoryTraversal(
        RootIdentity.capture(root),
        row_sink=sink,
        exclusion_policy=policy,
        progress=None,
        deterministic=True,
    )
    traversal.run()
    return {
        Path(observation.path).relative_to(root).as_posix()
        for observation in sink.rows
    }


def _populate(root: Path) -> None:
    for relative in (
        "mixed/AppData/docs/notes.md",
        "mixed/AppData/docs/metadata.json",
        "project/node_modules/pkg/package.json",
        "project/site-packages/pkg/metadata.json",
        "project/.venv/pyvenv.cfg",
        "project/__pycache__/module.pyc",
        "project/.pytest_cache/results.json",
        "project/tmp-generated/metadata.json",
        "project/.tmp-generated/notes.md",
        "project/standalone.pyc",
        "project/cache/metadata.json",
        "project/.git/config",
        "protected-content/secret.md",
    ):
        _write(root / relative)


def test_pre_admission_mode_observes_metadata_and_generated_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    root, _state, runtime, models, source, codex, _internal, _protected, _access = fixture
    _populate(root)
    legacy = _boundary(fixture, observe=False)
    observing = _boundary(fixture, observe=True)

    legacy_paths = _observed_paths(root, legacy.exclusion_policy)
    observing_paths = _observed_paths(root, observing.exclusion_policy)

    mixed_metadata = {
        "mixed/AppData/docs/notes.md",
        "mixed/AppData/docs/metadata.json",
    }
    generated_payloads = {
        "project/node_modules/pkg/package.json",
        "project/site-packages/pkg/metadata.json",
        "project/.venv/pyvenv.cfg",
        "project/__pycache__/module.pyc",
        "project/.pytest_cache/results.json",
        "project/tmp-generated/metadata.json",
        "project/.tmp-generated/notes.md",
        "project/standalone.pyc",
    }
    assert mixed_metadata <= legacy_paths
    assert mixed_metadata <= observing_paths
    assert generated_payloads <= observing_paths
    assert generated_payloads.isdisjoint(legacy_paths)
    assert "project/cache/metadata.json" in observing_paths
    assert "project/cache/metadata.json" in legacy_paths
    assert "project/.git/config" not in observing_paths
    assert "protected-content/secret.md" not in observing_paths

    # Explicit canonical roots remain excluded even when broad generated-name
    # pruning is disabled; their exact paths are not classifier input.
    for canonical_root in (codex, runtime, source):
        assert observing.exclusion_policy.excludes_directory(canonical_root)
    assert models.is_relative_to(runtime)


def test_observation_mode_preserves_vcs_and_symlink_no_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    root = fixture[0]
    _populate(root)
    target = root / "project" / "node_modules"
    alias = root / "project" / "alias-to-node-modules"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    observing = _boundary(fixture, observe=True)
    paths = _observed_paths(root, observing.exclusion_policy)

    assert "project/node_modules/pkg/package.json" in paths
    assert not any(path.startswith("project/alias-to-node-modules/") for path in paths)
    assert "project/.git/config" not in paths
    assert observing.exclusion_policy.excludes_directory(
        root / "project" / ".git", file_attributes=0
    )
    assert not observing.exclusion_policy.excludes_directory(
        root / "project" / "node_modules", file_attributes=0
    )
    assert not observing.exclusion_policy.excludes_file(root / "project" / "standalone.pyc")


def test_observation_mode_is_signature_bound_and_default_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    legacy = _boundary(fixture, observe=False)
    observing = _boundary(fixture, observe=True)

    assert legacy.exclusion_policy.directory_names != observing.exclusion_policy.directory_names
    assert legacy.exclusion_policy.directory_prefixes != observing.exclusion_policy.directory_prefixes
    assert legacy.exclusion_policy.directory_fragments != observing.exclusion_policy.directory_fragments
    assert legacy.exclusion_policy.file_suffixes != observing.exclusion_policy.file_suffixes
    assert legacy.exclusion_policy.signature != observing.exclusion_policy.signature
    assert legacy.effective_signature != observing.effective_signature
    assert observing.exclusion_policy.directory_names == frozenset({".git", ".hg", ".svn"})
    assert observing.exclusion_policy.directory_prefixes == ()
    assert observing.exclusion_policy.directory_fragments == ()
    assert observing.exclusion_policy.file_suffixes == ()

    with pytest.raises(TypeError, match="observe_regenerable_artifacts"):
        boundary_module.build_normal_inventory_boundary(
            fixture[0],
            fixture[1],
            access_policy=fixture[-1],
            internal_paths_policy=fixture[-3],
            protected_content_policy=fixture[-2],
            observe_regenerable_artifacts=1,
        )
