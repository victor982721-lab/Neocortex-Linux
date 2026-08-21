"""Registry and replay contracts for trusted-static architecture providers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import _04_Nucleo_Operativo.external_evidence_providers as providers_module
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile
from _04_Nucleo_Operativo.external_evidence_models import ExternalProviderBaseline
from _04_Nucleo_Operativo.external_evidence_providers import (
    COMPLEXIPY_COGNITIVE_PROVIDER_ID,
    GRIMP_ARCHITECTURE_PROVIDER_ID,
    RUFF_ANALYZE_PROVIDER_ID,
    VULTURE_UNUSED_PROVIDER_ID,
    ComplexipyCognitiveProvider,
    GrimpArchitectureProvider,
    RuffAnalyzeImportsProvider,
    VultureUnusedStaticProvider,
    providers_for_profile,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


def _external_file(path: Path, root: Path, version_id: int) -> ExternalEvidenceFile:
    observed = path.stat()
    digest = fingerprint_bytes(path.read_bytes())
    return ExternalEvidenceFile(
        version_id,
        str(path),
        path.relative_to(root).as_posix(),
        observed.st_size,
        observed.st_mtime_ns,
        digest.xxh3_128,
        digest.xxh3_64_guard,
    )


@pytest.mark.parametrize(
    ("provider_class", "provider_id"),
    (
        (RuffAnalyzeImportsProvider, RUFF_ANALYZE_PROVIDER_ID),
        (GrimpArchitectureProvider, GRIMP_ARCHITECTURE_PROVIDER_ID),
        (ComplexipyCognitiveProvider, COMPLEXIPY_COGNITIVE_PROVIDER_ID),
    ),
)
def test_architecture_providers_replay_exact_selected_inputs_without_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_class: type,
    provider_id: str,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    package = root / "_04_Nucleo_Operativo"
    package.mkdir(parents=True)
    scratch.mkdir()
    selected = package / "sample.py"
    excluded = root / "tests" / "helper.py"
    excluded.parent.mkdir()
    selected.write_text("VALUE = 1\n", encoding="utf-8")
    excluded.write_text("VALUE = 2\n", encoding="utf-8")
    files = (
        _external_file(selected, root, 1),
        _external_file(excluded, root, 2),
    )
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "test-1")
    provider = provider_class(root)
    observed_paths: list[tuple[str, ...]] = []

    def execute(_stage_root, staged, _environment):
        observed_paths.append(tuple(sorted(item.relative_path for item in staged.values())))
        return SimpleNamespace(
            findings=(),
            metrics=(),
            relations=(),
            stdout_bytes=2,
            stderr_bytes=0,
            process_invocations=1,
        )

    provider.executor = execute
    selected_signature = provider.baseline_input_signature(files)
    publication = provider.run(root, files, baseline=None, scratch_root=scratch)

    assert observed_paths == [("_04_Nucleo_Operativo/sample.py",)]
    assert publication.status == "completed"
    assert publication.descriptor.provider_id == provider_id
    assert publication.descriptor.scope == "production-packages-python-v1"
    assert publication.descriptor.project_configuration_digest is None
    assert publication.descriptor.loads_project_configuration is False
    assert tuple(item.relative_path for item in publication.inputs) == (
        "_04_Nucleo_Operativo/sample.py",
    )
    assert publication.counters["process_invocations"] == 1
    assert publication.input_signature == selected_signature

    excluded.write_text("VALUE = 3\n", encoding="utf-8")
    changed_excluded_files = (
        files[0],
        _external_file(excluded, root, 3),
    )
    assert provider.baseline_input_signature(changed_excluded_files) == selected_signature

    assert publication.result_digest is not None
    comparison_only = ExternalProviderBaseline(
        17,
        publication.descriptor.provider_id,
        publication.publication.tool_version,
        publication.input_signature,
        publication.descriptor.comparability_signature,
        publication.result_digest,
        (),
        (),
        (),
    )
    compared = provider.run(
        root,
        files,
        baseline=comparison_only,
        scratch_root=scratch,
    )

    assert compared.execution == "full"
    assert compared.replay_source_tool_run_id is None
    assert compared.counters["process_invocations"] == 1
    assert compared.counters["comparable"] == 1
    assert observed_paths == [
        ("_04_Nucleo_Operativo/sample.py",),
        ("_04_Nucleo_Operativo/sample.py",),
    ]

    baseline = ExternalProviderBaseline(
        17,
        publication.descriptor.provider_id,
        publication.publication.tool_version,
        publication.input_signature,
        publication.descriptor.comparability_signature,
        publication.result_digest,
        (),
        (),
        (),
        reuse_mode="exact_replay",
    )
    provider.executor = lambda *_args: pytest.fail("exact replay executed a provider")
    replay = provider.run(root, files, baseline=baseline, scratch_root=scratch)

    assert replay.execution == "cache_replay"
    assert replay.replay_source_tool_run_id == 17
    assert replay.counters["process_invocations"] == 0
    assert replay.counters["files_verified"] == 1
    assert replay.counters["cache_hits"] == 1


def test_vulture_provider_uses_exact_project_wide_input_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    package = root / "_04_Nucleo_Operativo"
    package.mkdir(parents=True)
    scratch.mkdir()
    package_file = package / "sample.py"
    root_file = root / "neocortex" / "public.py"
    root_file.parent.mkdir()
    package_file.write_text("VALUE = 1\n", encoding="utf-8")
    root_file.write_text("VALUE = 2\n", encoding="utf-8")
    files = (
        _external_file(package_file, root, 1),
        _external_file(root_file, root, 2),
    )
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "test-1")
    provider = VultureUnusedStaticProvider(root)
    observed_paths: list[tuple[str, ...]] = []
    limitations = (
        "vulture_confidence_below_100_is_heuristic",
        "static_name_analysis_cannot_prove_runtime_unused",
        "decorators_callbacks_registries_reexports_and_dynamic_access_require_correlation",
        "advisory_only_no_mutation_authority",
    )

    def execute(_stage_root, staged, _environment):
        observed_paths.append(tuple(sorted(item.relative_path for item in staged.values())))
        return SimpleNamespace(
            findings=(),
            stdout_bytes=2,
            stderr_bytes=0,
            process_invocations=1,
            limitations=limitations,
        )

    provider.executor = execute
    publication = provider.run(root, files, baseline=None, scratch_root=scratch)

    assert observed_paths == [("_04_Nucleo_Operativo/sample.py", "neocortex/public.py")]
    assert publication.status == "completed"
    assert publication.descriptor.provider_id == VULTURE_UNUSED_PROVIDER_ID
    assert publication.descriptor.scope == "current-inventory-python"
    assert publication.descriptor.project_configuration_digest is None
    assert publication.descriptor.loads_project_configuration is False
    assert publication.limitations == limitations
    assert tuple(item.relative_path for item in publication.inputs) == (
        "_04_Nucleo_Operativo/sample.py",
        "neocortex/public.py",
    )
    assert publication.counters["process_invocations"] == 1

    assert publication.result_digest is not None
    baseline = ExternalProviderBaseline(
        18,
        publication.descriptor.provider_id,
        publication.publication.tool_version,
        publication.input_signature,
        publication.descriptor.comparability_signature,
        publication.result_digest,
        (),
        (),
        (),
        reuse_mode="exact_replay",
    )
    provider.executor = lambda *_args: pytest.fail("exact replay executed Vulture")
    replay = provider.run(root, files, baseline=baseline, scratch_root=scratch)

    assert replay.execution == "cache_replay"
    assert replay.replay_source_tool_run_id == 18
    assert replay.counters["process_invocations"] == 0
    assert replay.counters["files_verified"] == 2
    assert replay.counters["cache_hits"] == 1
    assert replay.limitations == limitations


def test_trusted_static_registry_exposes_all_thirteen_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n[tool.pyright]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(providers_module, "_package_version", lambda name: f"{name}-test")
    monkeypatch.setattr(
        providers_module,
        "_pyright_locations",
        lambda: (Path("node"), Path("pyright.js"), "pyright-test"),
    )
    monkeypatch.setattr(
        providers_module,
        "_installed_distribution_signature",
        lambda **_kwargs: "installed-environment:fixture",
    )

    providers = providers_for_profile("trusted-static", root)

    assert tuple(item.descriptor.provider_id for item in providers) == (
        "ruff-protected-basic",
        "ruff-trusted-project",
        "mypy-trusted-project",
        "pyright-trusted-project",
        "semgrep-neocortex-invariants",
        "deptry-project-dependencies",
        "pip-audit-known-vulnerabilities",
        "installed-package-inventory",
        "vulture-unused-static",
        "ruff-analyze-imports",
        "grimp-architecture",
        "complexipy-cognitive",
        "git-history-local",
    )
