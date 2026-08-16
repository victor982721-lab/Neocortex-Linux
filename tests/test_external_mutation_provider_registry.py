"""Trusted-deep registry and replay contracts for focal Cosmic Ray evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

import _04_Nucleo_Operativo.external_evidence_providers as providers_module
import _04_Nucleo_Operativo.external_mutation_cosmic_ray as mutation_module
from _04_Nucleo_Operativo.code_contracts import (
    LEGACY_DEEP_CONFIGURATION_SCHEMA,
    deep_configuration_payload,
    deep_configuration_signature,
)
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile
from _04_Nucleo_Operativo.external_evidence_models import ExternalProviderBaseline
from _04_Nucleo_Operativo.external_evidence_providers import (
    COSMIC_RAY_MUTATION_PROVIDER_ID,
    CosmicRayFocalMutationProvider,
    provider_tool_versions,
    providers_for_profile,
)
from _04_Nucleo_Operativo.external_mutation_cosmic_ray import (
    FocalMutationExecution,
    mutation_input_signature,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


def _owner(root: Path, relative_path: str, version_id: int) -> ExternalEvidenceFile:
    path = root.joinpath(*relative_path.split("/"))
    metadata = path.stat()
    digest = fingerprint_bytes(path.read_bytes())
    return ExternalEvidenceFile(
        version_id,
        str(path),
        relative_path,
        metadata.st_size,
        metadata.st_mtime_ns,
        digest.xxh3_128,
        digest.xxh3_64_guard,
    )


def _project(tmp_path: Path) -> tuple[Path, Path, tuple[ExternalEvidenceFile, ...]]:
    root = tmp_path / "root"
    package = root / "pkg"
    tests = root / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    target = package / "logic.py"
    test = tests / "test_logic.py"
    target.write_text("def choose(value):\n    return 1 if value else 2\n", encoding="utf-8")
    test.write_text(
        "from pkg.logic import choose\n\ndef test_choose():\n    assert choose(True) == 1\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n[tool.pyright]\n",
        encoding="utf-8",
    )
    return root, target, (_owner(root, "pkg/logic.py", 1), _owner(root, "tests/test_logic.py", 2))


def _deep_payload(*, target: str | None = "pkg/logic.py") -> tuple[dict[str, object], str]:
    mutation: dict[str, object] = {}
    if target is not None:
        mutation = {
            "mutation_target": target,
            "mutation_symbol": "pkg.logic.choose",
            "mutation_max_mutants": 4,
            "mutation_timeout_seconds": 10,
            "mutation_time_budget_seconds": 60,
        }
    payload = deep_configuration_payload(
        analysis_profile="trusted-deep",
        test_selectors=("tests/test_logic.py::test_choose",),
        max_tests=10,
        time_budget_seconds=60,
        shard_size=5,
        **mutation,
    )
    return payload, deep_configuration_signature(payload)


def _legacy_payload() -> tuple[dict[str, object], str]:
    current, _signature = _deep_payload(target=None)
    legacy = {
        key: value
        for key, value in current.items()
        if not key.startswith("mutation_") and key != "schema"
    }
    legacy["schema"] = LEGACY_DEEP_CONFIGURATION_SCHEMA
    return legacy, deep_configuration_signature(legacy)


def _baseline(publication) -> ExternalProviderBaseline:
    assert publication.result_digest is not None
    return ExternalProviderBaseline(
        71,
        publication.descriptor.provider_id,
        publication.publication.tool_version,
        publication.input_signature,
        publication.descriptor.comparability_signature,
        publication.result_digest,
        tuple(item.portable_finding_id for item in publication.findings),
        tuple(item.portable_metric_id for item in publication.metrics),
        tuple(item.portable_relation_id for item in publication.relations),
        reuse_mode="exact_replay",
    )


def test_full_stages_exact_copy_and_replay_preserves_adapter_costs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, target, files = _project(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    legacy_namespace = scratch / "m"
    legacy_namespace.mkdir(mode=0o775)
    legacy_namespace.chmod(0o775)
    legacy_sentinel = legacy_namespace / "legacy-state"
    legacy_sentinel.write_text("untrusted legacy data", encoding="utf-8")
    payload, signature = _deep_payload()
    monkeypatch.setattr(providers_module, "cosmic_ray_tool_version", lambda: "8.4.6")
    executable_path = str(tmp_path / "trusted-tools")
    executable_extensions = ".EXE;.CMD"
    home_directory = tmp_path / "trusted-home"
    home_directory.mkdir()
    monkeypatch.setattr(
        providers_module,
        "trusted_deep_home_directory",
        lambda: str(home_directory),
    )
    monkeypatch.setenv("PATH", executable_path)
    monkeypatch.setenv("PATHEXT", executable_extensions)
    observed_stage_roots: list[Path] = []
    observed_scratch_roots: list[Path] = []

    def execute(stage_root, staged, environment, *, trusted_root, scratch_root, config):
        observed_stage_roots.append(stage_root)
        observed_scratch_roots.append(scratch_root)
        assert trusted_root == root
        assert scratch_root.is_dir()
        assert environment["PATH"] == executable_path
        assert environment["PATHEXT"] == executable_extensions
        assert environment["HOME"] == str(home_directory)
        if providers_module.os.name == "nt":
            assert environment["USERPROFILE"] == str(home_directory)
        assert config.target_relative_path == "pkg/logic.py"
        assert tuple(sorted(item.relative_path for item in staged.values())) == (
            "pkg/logic.py",
            "tests/test_logic.py",
        )
        for staged_path, owner in staged.items():
            assert Path(staged_path).is_relative_to(stage_root / "source")
            assert Path(staged_path).read_bytes() == Path(owner.path).read_bytes()
        scope = mutation_input_signature(tuple(staged.values()), config)
        return FocalMutationExecution(
            (),
            (),
            (),
            321,
            7,
            5,
            {
                "process_invocations": 5,
                "mutants_generated": 4,
                "mutants_selected": 4,
                "mutants_completed": 4,
                "mutants_killed": 3,
                "mutants_survived": 1,
                "wall_milliseconds": 55,
                "measurement_complete": 1,
            },
            ("focal_declared_target_and_tests_only",),
            scope,
            True,
        )

    provider = CosmicRayFocalMutationProvider(
        root,
        payload,
        signature,
        executor=execute,
    )
    original = target.read_bytes()
    first = provider.run(root, files, baseline=None, scratch_root=scratch)

    assert first.status == "completed"
    assert first.execution == "full"
    assert first.coverage_complete is True
    assert first.counters["process_invocations"] == 5
    assert first.counters["stdout_bytes"] == 321
    assert first.counters["stderr_bytes"] == 7
    assert first.counters["bytes_staged"] == sum(item.size for item in files)
    assert first.counters["mutants_killed"] == 3
    assert first.publication.provenance["mutation_execution"]["uses_network"] is True
    assert target.read_bytes() == original
    assert observed_stage_roots and not observed_stage_roots[0].exists()
    assert len(observed_scratch_roots) == 1
    assert observed_scratch_roots[0].parent == scratch / "m2"
    assert observed_scratch_roots[0].stat().st_mode & 0o777 == 0o700
    assert (scratch / "m2").stat().st_mode & 0o777 == 0o700
    assert legacy_namespace.stat().st_mode & 0o777 == 0o775
    assert legacy_sentinel.read_text(encoding="utf-8") == "untrusted legacy data"

    provider.executor = lambda *_args, **_kwargs: pytest.fail("exact replay executed mutation")
    replay = provider.run(root, files, baseline=_baseline(first), scratch_root=scratch)

    assert replay.status == "skipped"
    assert replay.execution == "cache_replay"
    assert replay.result_digest == first.result_digest
    assert replay.replay_source_tool_run_id == 71
    assert replay.counters["process_invocations"] == 0
    assert replay.counters["bytes_staged"] == 0
    assert replay.counters["files_verified"] == 2
    assert replay.counters["bytes_verified"] == sum(item.size for item in files)


def test_mutation_environment_signature_covers_executable_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _target, _files = _project(tmp_path)
    payload, signature = _deep_payload()
    monkeypatch.setattr(providers_module, "cosmic_ray_tool_version", lambda: "8.4.6")
    monkeypatch.setenv("PATH", str(tmp_path / "tools-a"))
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    first = CosmicRayFocalMutationProvider(root, payload, signature)

    monkeypatch.setenv("PATH", str(tmp_path / "tools-b"))
    second = CosmicRayFocalMutationProvider(root, payload, signature)

    assert first.descriptor.environment_signature != second.descriptor.environment_signature


def test_real_provider_baseline_preserves_git_executable_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if mutation_module.cosmic_ray_tool_version() != "8.4.6":
        pytest.skip("Cosmic Ray 8.4.6 is not installed in the active test runtime")
    root = tmp_path / "root"
    package = root / "pkg"
    tests = root / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    target = package / "logic.py"
    target.write_text("def choose(value):\n    return 1 if value else 2\n", encoding="utf-8")
    test = tests / "test_logic.py"
    test.write_text(
        "import shutil\n\n"
        "from pkg.logic import choose\n\n"
        "def test_choose_and_git_discovery():\n"
        "    assert shutil.which('git') is not None\n"
        "    assert choose(True) == 1\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n[tool.pyright]\n",
        encoding="utf-8",
    )
    files = (_owner(root, "pkg/logic.py", 1), _owner(root, "tests/test_logic.py", 2))
    payload = deep_configuration_payload(
        analysis_profile="trusted-deep",
        test_selectors=("tests/test_logic.py::test_choose_and_git_discovery",),
        max_tests=1,
        time_budget_seconds=60,
        shard_size=1,
        mutation_target="pkg/logic.py",
        mutation_symbol="pkg.logic.choose",
        mutation_max_mutants=2,
        mutation_timeout_seconds=10,
        mutation_time_budget_seconds=60,
    )
    signature = deep_configuration_signature(payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = target.read_bytes()
    monkeypatch.setattr(mutation_module, "_canonical_repository_root", lambda: root)
    publication = CosmicRayFocalMutationProvider(root, payload, signature).run(
        root,
        files,
        baseline=None,
        scratch_root=scratch,
    )

    assert publication.status == "completed", publication.publication.provenance.get("error")
    assert publication.execution == "full"
    assert publication.coverage_complete is True
    assert publication.counters["process_invocations"] >= 2
    assert target.read_bytes() == before


def test_legacy_and_missing_target_abstain_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _target, files = _project(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(providers_module, "cosmic_ray_tool_version", lambda: "8.4.6")
    legacy, legacy_signature = _legacy_payload()
    legacy_provider = CosmicRayFocalMutationProvider(root, legacy, legacy_signature)
    legacy_provider.executor = lambda *_args, **_kwargs: pytest.fail("legacy config executed")

    legacy_result = legacy_provider.run(
        root,
        files,
        baseline=None,
        scratch_root=scratch,
    )

    assert legacy_result.status == "skipped"
    assert legacy_result.execution == "skipped"
    assert legacy_result.coverage_complete is False
    assert legacy_result.counters["process_invocations"] == 0
    assert legacy_result.counters["errors"] == 0
    assert legacy_result.limitations == ("mutation_not_declared_in_legacy_deep_configuration",)

    payload, signature = _deep_payload()
    missing_provider = CosmicRayFocalMutationProvider(root, payload, signature)
    missing_provider.executor = lambda *_args, **_kwargs: pytest.fail("missing target executed")
    missing_files = tuple(item for item in files if item.relative_path != "pkg/logic.py")
    missing = missing_provider.run(
        root,
        missing_files,
        baseline=None,
        scratch_root=scratch,
    )
    assert missing.execution == "skipped"
    assert missing.limitations == ("mutation_target_not_indexed",)
    assert missing.counters["process_invocations"] == 0


def test_provider_is_registered_only_for_trusted_deep_and_reports_exact_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _target, _files = _project(tmp_path)
    payload, signature = _deep_payload()
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
    monkeypatch.setattr(
        providers_module,
        "_git_tool_probe",
        lambda: (Path("git"), "2.fixture"),
    )
    monkeypatch.setattr(providers_module, "cosmic_ray_tool_version", lambda: "8.4.6")

    protected = providers_for_profile("protected", root)
    static = providers_for_profile("trusted-static", root)
    deep = providers_for_profile(
        "trusted-deep",
        root,
        deep_configuration=payload,
        deep_configuration_signature=signature,
    )
    protected_ids = {item.descriptor.provider_id for item in protected}
    static_ids = {item.descriptor.provider_id for item in static}
    deep_ids = {item.descriptor.provider_id for item in deep}
    mutation = next(
        item for item in deep if item.descriptor.provider_id == COSMIC_RAY_MUTATION_PROVIDER_ID
    )

    assert COSMIC_RAY_MUTATION_PROVIDER_ID not in protected_ids
    assert COSMIC_RAY_MUTATION_PROVIDER_ID not in static_ids
    assert COSMIC_RAY_MUTATION_PROVIDER_ID in deep_ids
    assert len(static) == 13
    assert len(deep) == 15
    assert mutation.descriptor.profile == "trusted-deep"
    assert mutation.descriptor.trust_requirement == "trusted-execution"
    assert mutation.descriptor.invalidation_strategy == "dynamic_suite"
    assert mutation.descriptor.imports_content is True
    assert mutation.descriptor.executes_content is True
    assert mutation.descriptor.uses_network is True
    assert mutation.descriptor.mutation_authority is False
    assert provider_tool_versions()[COSMIC_RAY_MUTATION_PROVIDER_ID] == "8.4.6"


def test_legacy_deep_registry_constructs_abstaining_mutation_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _target, _files = _project(tmp_path)
    legacy, signature = _legacy_payload()
    monkeypatch.setattr(providers_module, "cosmic_ray_tool_version", lambda: "8.4.6")
    providers = providers_for_profile(
        "trusted-deep",
        root,
        deep_configuration=legacy,
        deep_configuration_signature=signature,
    )
    mutation = next(
        item for item in providers if item.descriptor.provider_id == COSMIC_RAY_MUTATION_PROVIDER_ID
    )

    assert isinstance(mutation, CosmicRayFocalMutationProvider)
    assert mutation.config is None
