"""Behavioral contract for canonical NeoCortex-owned paths."""
# region [00] Contexto del módulo
# Módulo: tests/test_internal_paths_policy.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.internal_paths as internal_paths_module
from _04_Nucleo_Operativo.app_paths import local_application_data_directory
from _04_Nucleo_Operativo.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
    ProtectedAnalysisRootError,
)
from _04_Nucleo_Operativo.internal_paths import (
    InternalPathIdentity,
    InternalPathProtectionError,
    InternalPathSpec,
    InternalPathsPolicy,
    effective_inventory_policy_signature,
)
# endregion [01]

# region [02] Implementación


@dataclass(frozen=True, slots=True)
class _Layout:
    profile: Path
    repository: Path
    runtime: Path
    application_data: Path
    self_analysis: Path
    launcher: Path


def _create_layout(tmp_path: Path) -> _Layout:
    profile = tmp_path / "profile"
    repository = profile / "Neocortex" / "Repository"
    runtime = profile / "AppData" / "Local" / "Programs" / "Neocortex"
    application_data = profile / "AppData" / "Local" / "Neocortex"
    self_analysis = application_data / "self-analysis"
    launcher = runtime / "bin" / "Neocortex.exe"
    repository.mkdir(parents=True)
    self_analysis.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    return _Layout(
        profile,
        repository,
        runtime,
        application_data,
        self_analysis,
        launcher,
    )


def _specs(layout: _Layout) -> tuple[InternalPathSpec, ...]:
    return (
        InternalPathSpec("repository", "tree", layout.repository),
        InternalPathSpec("runtime", "tree", layout.runtime),
        InternalPathSpec(
            "application_data",
            "tree",
            layout.application_data,
        ),
        InternalPathSpec("self_analysis", "tree", layout.self_analysis),
        InternalPathSpec("launcher", "file", layout.launcher),
    )


def _capture(layout: _Layout) -> InternalPathsPolicy:
    return InternalPathsPolicy.capture(_specs(layout))


def test_policy_signature_and_manifest_are_deterministic(tmp_path: Path) -> None:
    layout = _create_layout(tmp_path)
    first = _capture(layout)
    second = _capture(layout)

    assert first == second
    assert first.signature.startswith("internal-paths-policy-v1:xxh3_128:")
    assert len(first.signature.rsplit(":", 1)[1]) == 32
    assert first.manifest() == second.manifest()
    assert [entry["role"] for entry in first.manifest()["entries"]] == [
        "application_data",
        "launcher",
        "repository",
        "runtime",
        "self_analysis",
    ]


def test_capture_rejects_an_unbounded_spec_source_after_six_items(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    specs = _specs(layout)
    consumed: list[int] = []

    def unbounded_specs():
        for spec in specs:
            consumed.append(len(consumed))
            yield spec
        while True:
            consumed.append(len(consumed))
            yield specs[0]

    with pytest.raises(ValueError, match="entry count"):
        InternalPathsPolicy.capture(unbounded_specs())

    assert len(consumed) == 6


def test_existing_internal_alias_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _create_layout(tmp_path)
    redirected = layout.profile / "redirected-repository"
    redirected.mkdir()
    real_realpath = os.path.realpath
    repository_key = os.path.normcase(os.path.abspath(layout.repository))

    def redirect_repository(path: str | os.PathLike[str]) -> str:
        if os.path.normcase(os.path.abspath(path)) == repository_key:
            return str(redirected)
        return real_realpath(path)

    monkeypatch.setattr(
        internal_paths_module.os.path,
        "realpath",
        redirect_repository,
    )

    with pytest.raises(ValueError, match="alias or reparse"):
        InternalPathIdentity.capture("repository", "tree", layout.repository)


def test_missing_internal_reservation_with_aliased_prefix_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _create_layout(tmp_path)
    reserved = layout.profile / "future" / "Repository"
    redirected = layout.profile / "elsewhere" / "Repository"
    original_physical = internal_paths_module._physical_normalized

    def redirect_reserved(path: str | os.PathLike[str]) -> Path:
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(reserved)):
            return redirected
        return original_physical(path)

    monkeypatch.setattr(
        internal_paths_module,
        "_physical_normalized",
        redirect_reserved,
    )

    with pytest.raises(ValueError, match="alias or reparse"):
        InternalPathIdentity.capture("repository", "tree", reserved)


def test_normal_profile_root_prunes_internal_trees_but_internal_root_fails(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)
    profile_access = CorpusAccessPolicy.capture("normal", layout.profile)

    policy.validate_corpus_access(profile_access)
    assert set(policy.inventory_exclusion_roots(profile_access)) == {
        layout.repository,
        layout.runtime,
        layout.application_data,
    }

    with pytest.raises(InternalPathProtectionError, match="normal corpus root"):
        policy.validate_corpus_access(CorpusAccessPolicy.capture("normal", layout.repository))
    child = layout.repository / "package"
    child.mkdir()
    with pytest.raises(InternalPathProtectionError, match="normal corpus root"):
        policy.validate_corpus_access(CorpusAccessPolicy.capture("normal", child))


def test_analyze_only_allows_exact_repository_or_disjoint_external_root(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)
    external = tmp_path / "external-source"
    external.mkdir()

    policy.validate_corpus_access(CorpusAccessPolicy.capture("analyze_only", layout.repository))
    policy.validate_corpus_access(CorpusAccessPolicy.capture("analyze_only", external))

    with pytest.raises(
        InternalPathProtectionError,
        match="non-repository internal path",
    ):
        policy.validate_corpus_access(CorpusAccessPolicy.capture("analyze_only", layout.profile))
    with pytest.raises(
        InternalPathProtectionError,
        match="non-repository internal path",
    ):
        policy.validate_corpus_access(
            CorpusAccessPolicy.capture("analyze_only", layout.application_data)
        )


def test_mutation_guard_rejects_internal_paths_and_their_ancestors(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)
    ordinary = tmp_path / "ordinary" / "document.pdf"

    policy.require_mutation_paths_allowed(ordinary)
    for blocked in (
        layout.profile,
        layout.repository,
        layout.repository / "module.py",
        layout.launcher,
        layout.application_data / "state" / "framework.sqlite3",
    ):
        with pytest.raises(
            InternalPathProtectionError,
            match="internal_framework_root",
        ):
            policy.require_mutation_paths_allowed(blocked)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-path contract")
def test_normal_composite_guard_rejects_internal_extended_alias(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    guard = CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", layout.profile),
        _capture(layout),
    )
    alias = Path("\\\\?\\" + os.path.abspath(layout.repository / "module.py"))

    guard.require_paths_allowed(tmp_path / "ordinary" / "document.pdf")
    with pytest.raises(InternalPathProtectionError, match="internal repository"):
        guard.require_paths_allowed(alias)


def test_missing_reservation_appearance_changes_policy_and_fails_old_guard(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    layout.launcher.unlink()
    before = _capture(layout)

    layout.launcher.write_bytes(b"new launcher")
    with pytest.raises(
        InternalPathProtectionError,
        match="reserved internal path appeared",
    ):
        before.verify_identities()

    after = _capture(layout)
    assert after.signature != before.signature


def test_replaced_internal_directory_fails_physical_identity_check(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)
    original = layout.repository.with_name("Repository-old")
    layout.repository.rename(original)
    layout.repository.mkdir()

    with pytest.raises(
        InternalPathProtectionError,
        match="internal path identity changed",
    ):
        policy.verify_identities()


def test_policy_requires_complete_valid_roles_and_disjoint_repository(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    with pytest.raises(ValueError, match="every role exactly once"):
        InternalPathsPolicy.capture(_specs(layout)[:-1])

    overlapping_self_analysis = layout.repository / "self-analysis"
    overlapping_self_analysis.mkdir()
    overlapping = _Layout(
        layout.profile,
        layout.repository,
        layout.runtime,
        layout.repository,
        overlapping_self_analysis,
        layout.launcher,
    )
    with pytest.raises(ValueError, match="repository must be disjoint"):
        InternalPathsPolicy.capture(_specs(overlapping))


def test_external_hardlink_to_launcher_is_still_protected(tmp_path: Path) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)
    alias = tmp_path / "launcher-hardlink.exe"
    os.link(layout.launcher, alias)

    with pytest.raises(
        InternalPathProtectionError,
        match="internal launcher",
    ):
        policy.require_mutation_paths_allowed(alias)


def test_normal_root_identity_is_revalidated_by_internal_policy(
    tmp_path: Path,
) -> None:
    layout = _create_layout(tmp_path)
    policy = _capture(layout)
    access = CorpusAccessPolicy.capture("normal", layout.profile)
    original = layout.profile.with_name("profile-old")
    layout.profile.rename(original)
    layout.profile.mkdir()

    with pytest.raises(ProtectedAnalysisRootError, match="identity changed"):
        policy.validate_corpus_access(access)


def test_relative_local_appdata_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", "relative-local-appdata")

    with pytest.raises(ValueError, match="must be absolute"):
        local_application_data_directory()


def test_effective_inventory_signature_binds_both_layers() -> None:
    first = effective_inventory_policy_signature(
        "inventory-exclusion-policy-v1:xxh3_128:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "internal-paths-policy-v1:xxh3_128:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    repeated = effective_inventory_policy_signature(
        "inventory-exclusion-policy-v1:xxh3_128:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "internal-paths-policy-v1:xxh3_128:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    changed = effective_inventory_policy_signature(
        "inventory-exclusion-policy-v1:xxh3_128:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "internal-paths-policy-v1:xxh3_128:cccccccccccccccccccccccccccccccc",
    )
    protected = effective_inventory_policy_signature(
        "inventory-exclusion-policy-v1:xxh3_128:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "internal-paths-policy-v1:xxh3_128:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "protected-content-policy-v1:xxh3_128:dddddddddddddddddddddddddddddddd",
    )
    protected_repeated = effective_inventory_policy_signature(
        "inventory-exclusion-policy-v1:xxh3_128:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "internal-paths-policy-v1:xxh3_128:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "protected-content-policy-v1:xxh3_128:dddddddddddddddddddddddddddddddd",
    )
    protected_changed = effective_inventory_policy_signature(
        "inventory-exclusion-policy-v1:xxh3_128:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "internal-paths-policy-v1:xxh3_128:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "protected-content-policy-v1:xxh3_128:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    )

    assert first == repeated
    assert first.startswith("effective-inventory-policy-v1:xxh3_128:")
    assert first != changed
    assert protected == protected_repeated
    assert protected.startswith("effective-inventory-policy-v2:xxh3_128:")
    assert protected not in {first, protected_changed}


# endregion [02]
