"""Behavioral contract for identity-bound immutable user content."""
# region [00] Contexto del módulo
# Módulo: tests/test_protected_content_policy.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

import neocortex.safety.protected_content as protected_content_module
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    ProtectedAnalysisRootError,
)
from neocortex.safety.protected_content import (
    MAX_PROTECTED_PATH_ENTRIES,
    ProtectedContentError,
    ProtectedContentPolicy,
    ProtectedPathIdentity,
    ProtectedPathSpec,
    canonical_protected_content_policy,
)
# endregion [01]

# region [02] Implementación


@dataclass(frozen=True, slots=True)
class _Layout:
    profile: Path
    documents: Path
    documents_codex: Path
    codex: Path
    appdata: Path
    cache: Path
    denybin: Path


def _create_layout(tmp_path: Path) -> _Layout:
    profile = tmp_path / "profile"
    documents = profile / "Documents"
    documents_codex = documents / "Codex"
    codex = profile / ".codex"
    appdata = profile / "AppData"
    cache = profile / ".cache"
    denybin = profile / ".sbx-denybin"
    documents_codex.joinpath("project").mkdir(parents=True)
    for name in (
        "sessions",
        "archived_sessions",
        "memories",
        "skills",
        "scripts",
        "hooks",
        "visualizations",
        "operational-secret",
    ):
        (codex / name).mkdir(parents=True)
    (codex / "sessions" / "2026").mkdir()
    (codex / "AGENTS.md").write_text("safe instructions", encoding="utf-8")
    (codex / "AGENTS.override.md").write_text(
        "safe local instructions",
        encoding="utf-8",
    )
    appdata.mkdir()
    cache.mkdir()
    denybin.mkdir()
    return _Layout(
        profile,
        documents,
        documents_codex,
        codex,
        appdata,
        cache,
        denybin,
    )


def _capture(layout: _Layout) -> ProtectedContentPolicy:
    return canonical_protected_content_policy(
        home=layout.profile,
        documents=layout.documents,
    )


def _entry(
    policy: ProtectedContentPolicy,
    role: str,
) -> ProtectedPathIdentity:
    return next(entry for entry in policy.entries if entry.role == role)


def test_signature_and_manifest_are_deterministic(tmp_path: Path) -> None:
    layout = _create_layout(tmp_path)
    first = _capture(layout)
    second = _capture(layout)

    assert first == second
    assert first.signature.startswith("protected-content-policy-v1:xxh3_128:")
    assert len(first.signature.rsplit(":", 1)[1]) == 32
    assert first.manifest() == second.manifest()
    roles = [entry["role"] for entry in first.manifest()["entries"]]
    assert roles == sorted(roles)


def test_canonical_factory_captures_only_declared_safe_codex_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _create_layout(tmp_path)

    def unexpected_known_folder_lookup() -> Path:
        raise AssertionError("injected Documents path must be authoritative")

    monkeypatch.setattr(
        protected_content_module,
        "_windows_documents_directory",
        unexpected_known_folder_lookup,
    )
    policy = _capture(layout)
    by_role = {entry.role: entry for entry in policy.entries}

    assert set(by_role) == {
        "application_data",
        "codex_agents",
        "codex_agents_override",
        "codex_archived_sessions",
        "codex_home",
        "codex_hooks",
        "codex_memories",
        "codex_runtime_cache",
        "codex_sandbox_denybin",
        "codex_scripts",
        "codex_sessions",
        "codex_skills",
        "codex_visualizations",
        "documents_codex",
    }
    assert by_role["codex_home"].disposition == "exclude"
    assert by_role["codex_agents"].kind == "file"
    assert by_role["codex_agents"].disposition == "analyze_read_only"
    assert by_role["codex_agents_override"].canonical_path == (
        layout.codex / "AGENTS.override.md"
    )
    assert by_role["documents_codex"].canonical_path == layout.documents_codex
    assert by_role["application_data"].canonical_path == layout.appdata


def test_exact_codex_container_and_allowlisted_descendants_are_read_only(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)

    codex_access = CorpusAccessPolicy.capture("normal", layout.codex)
    sessions_access = CorpusAccessPolicy.capture(
        "normal",
        layout.codex / "sessions",
    )
    nested_access = CorpusAccessPolicy.capture(
        "normal",
        layout.codex / "sessions" / "2026",
    )

    policy.validate_corpus_access(codex_access)
    policy.validate_corpus_access(sessions_access)
    policy.validate_corpus_access(nested_access)
    assert policy.run_is_read_only(codex_access)
    assert policy.run_is_read_only(sessions_access)
    assert policy.run_is_read_only(nested_access)


def test_unlisted_codex_descendant_and_appdata_roots_are_rejected(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)

    for path in (
        layout.codex / "operational-secret",
        layout.appdata,
    ):
        access = CorpusAccessPolicy.capture("normal", path)
        with pytest.raises(
            ProtectedContentError,
            match="protected_content_root",
        ):
            policy.validate_corpus_access(access)

    appdata_child = layout.appdata / "Local"
    appdata_child.mkdir()
    with pytest.raises(ProtectedContentError, match="excluded protected content"):
        policy.validate_corpus_access(
            CorpusAccessPolicy.capture("normal", appdata_child)
        )


def test_profile_root_is_allowed_and_returns_minimal_exclusions(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)
    access = CorpusAccessPolicy.capture("normal", layout.profile)

    policy.validate_corpus_access(access)

    assert not policy.run_is_read_only(access)
    assert set(policy.inventory_exclusion_roots(access)) == {
        layout.appdata,
        layout.cache,
        layout.denybin,
    }
    assert layout.codex not in policy.inventory_exclusion_roots(access)


def test_documents_codex_root_and_children_are_read_only(tmp_path: Path) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)

    assert policy.run_is_read_only(
        CorpusAccessPolicy.capture("normal", layout.documents_codex)
    )
    assert policy.run_is_read_only(
        CorpusAccessPolicy.capture(
            "normal",
            layout.documents_codex / "project",
        )
    )
    assert not policy.run_is_read_only(
        CorpusAccessPolicy.capture("normal", layout.documents)
    )


def test_analyze_only_mode_is_read_only_even_for_disjoint_content(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    policy = _capture(layout)

    assert policy.run_is_read_only(CorpusAccessPolicy.capture("analyze_only", external))


def test_every_entry_is_a_symmetric_mutation_boundary(tmp_path: Path) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)
    external = tmp_path / "external" / "ordinary.txt"

    policy.require_mutation_paths_allowed(None, external)
    for blocked in (
        layout.profile,
        layout.documents_codex,
        layout.documents_codex / "project" / "file.md",
        layout.codex,
        layout.codex / "sessions" / "2026" / "session.jsonl",
        layout.codex / "AGENTS.md",
        layout.appdata / "Local" / "state.db",
        layout.cache / "runtime",
        layout.denybin / "ssh.cmd",
    ):
        with pytest.raises(ProtectedContentError, match="protected_content_root"):
            policy.require_mutation_paths_allowed(blocked)


def test_missing_reservation_appearance_invalidates_old_policy(
    tmp_path: Path,
) -> None:
    reserved = tmp_path / "profile" / ".cache"
    reserved.parent.mkdir()
    policy = ProtectedContentPolicy.capture(
        (ProtectedPathSpec("cache", "tree", "exclude", reserved),)
    )

    assert not _entry(policy, "cache").exists
    reserved.mkdir()
    with pytest.raises(
        ProtectedContentError,
        match="reserved protected path appeared",
    ):
        policy.verify_identities()


def test_replaced_protected_directory_fails_identity_verification(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "protected",
                "tree",
                "analyze_read_only",
                protected,
            ),
        )
    )
    original = tmp_path / "protected-old"
    protected.rename(original)
    protected.mkdir()

    with pytest.raises(ProtectedContentError, match="identity changed"):
        policy.verify_identities()


def test_external_hardlink_to_protected_file_is_blocked(tmp_path: Path) -> None:
    protected = tmp_path / "AGENTS.md"
    alias = tmp_path / "alias.md"
    protected.write_text("instructions", encoding="utf-8")
    os.link(protected, alias)
    policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "agents",
                "file",
                "analyze_read_only",
                protected,
            ),
        )
    )

    with pytest.raises(ProtectedContentError, match="protected content agents"):
        policy.require_mutation_paths_allowed(alias)


def test_capture_rejects_relative_paths_and_wrong_object_kinds(
    tmp_path: Path,
) -> None:
    regular_file = tmp_path / "regular.txt"
    regular_file.write_text("content", encoding="utf-8")

    with pytest.raises(ValueError, match="must be absolute"):
        ProtectedPathIdentity.capture(
            "relative",
            "tree",
            "exclude",
            Path("relative"),
        )
    with pytest.raises(NotADirectoryError):
        ProtectedPathIdentity.capture(
            "wrong-kind",
            "tree",
            "exclude",
            regular_file,
        )


def test_existing_and_missing_aliases_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = tmp_path / "protected"
    redirected = tmp_path / "redirected"
    protected.mkdir()
    redirected.mkdir()
    real_realpath = os.path.realpath
    protected_key = os.path.normcase(os.path.abspath(protected))

    def redirect_existing(path: str | os.PathLike[str]) -> str:
        if os.path.normcase(os.path.abspath(path)) == protected_key:
            return str(redirected)
        return real_realpath(path)

    monkeypatch.setattr(
        protected_content_module.os.path,
        "realpath",
        redirect_existing,
    )
    with pytest.raises(ValueError, match="alias or reparse"):
        ProtectedPathIdentity.capture(
            "protected",
            "tree",
            "exclude",
            protected,
        )


def test_duplicate_roles_paths_and_file_aliases_are_rejected(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with pytest.raises(ValueError, match="roles must be unique"):
        ProtectedContentPolicy.capture(
            (
                ProtectedPathSpec("same", "tree", "exclude", first),
                ProtectedPathSpec(
                    "same",
                    "tree",
                    "analyze_read_only",
                    second,
                ),
            )
        )
    with pytest.raises(ValueError, match="paths must be unique"):
        ProtectedContentPolicy.capture(
            (
                ProtectedPathSpec("first", "tree", "exclude", first),
                ProtectedPathSpec(
                    "second",
                    "tree",
                    "analyze_read_only",
                    first,
                ),
            )
        )

    original = tmp_path / "original.txt"
    alias = tmp_path / "alias.txt"
    original.write_text("same object", encoding="utf-8")
    os.link(original, alias)
    with pytest.raises(ValueError, match="file aliases are ambiguous"):
        ProtectedContentPolicy.capture(
            (
                ProtectedPathSpec("original", "file", "exclude", original),
                ProtectedPathSpec(
                    "alias",
                    "file",
                    "analyze_read_only",
                    alias,
                ),
            )
        )


def test_capture_bounds_iterables_without_consuming_forever(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    spec = ProtectedPathSpec("same", "tree", "exclude", protected)
    consumed: list[int] = []

    def unbounded_specs():
        while True:
            consumed.append(len(consumed))
            yield spec

    with pytest.raises(ValueError, match="entry count"):
        ProtectedContentPolicy.capture(unbounded_specs())

    assert len(consumed) == MAX_PROTECTED_PATH_ENTRIES + 1


def test_forged_signature_and_unsorted_entries_are_rejected(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec("a", "tree", "exclude", first),
            ProtectedPathSpec("b", "tree", "exclude", second),
        )
    )

    with pytest.raises(ValueError, match="inconsistent with its signature"):
        ProtectedContentPolicy(policy.entries, "forged")
    with pytest.raises(ValueError, match="inconsistent with its signature"):
        ProtectedContentPolicy(tuple(reversed(policy.entries)), policy.signature)


def test_most_specific_disposition_wins_for_access_and_inventory(
    tmp_path: Path,
) -> None:
    container = tmp_path / "container"
    allowed = container / "allowed"
    nested_excluded = allowed / "private"
    nested_excluded.mkdir(parents=True)
    policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec("container", "tree", "exclude", container),
            ProtectedPathSpec(
                "allowed",
                "tree",
                "analyze_read_only",
                allowed,
            ),
            ProtectedPathSpec(
                "nested-excluded",
                "tree",
                "exclude",
                nested_excluded,
            ),
        )
    )

    container_access = CorpusAccessPolicy.capture("normal", container)
    allowed_access = CorpusAccessPolicy.capture("normal", allowed)
    policy.validate_corpus_access(container_access)
    assert policy.run_is_read_only(container_access)
    assert policy.run_is_read_only(allowed_access)
    assert policy.inventory_exclusion_roots(allowed_access) == (nested_excluded,)
    with pytest.raises(ProtectedContentError, match="excluded protected content"):
        policy.validate_corpus_access(
            CorpusAccessPolicy.capture("normal", nested_excluded)
        )


def test_policy_revalidates_the_corpus_root_identity(tmp_path: Path) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)
    access = CorpusAccessPolicy.capture("normal", layout.profile)
    old_profile = tmp_path / "profile-old"
    layout.profile.rename(old_profile)
    layout.profile.mkdir()

    with pytest.raises(ProtectedAnalysisRootError, match="identity changed"):
        policy.validate_corpus_access(access)


def test_error_is_compatible_with_existing_protected_root_boundary() -> None:
    error = ProtectedContentError("blocked")

    assert isinstance(error, ProtectedAnalysisRootError)
    assert error.reason_code == "protected_content_root"
    assert str(error) == "protected_content_root: blocked"
# endregion [02]
