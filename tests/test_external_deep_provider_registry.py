"""Registry, replay and runtime contracts for trusted-deep evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import _04_Nucleo_Operativo.code_route as code_route_module
import _04_Nucleo_Operativo.external_deep_coverage as deep_module
import _04_Nucleo_Operativo.external_evidence_providers as providers_module
import _04_Nucleo_Operativo.external_evidence_store as store_module
from _03_Progreso import RecordingProgress
from _04_Nucleo_Operativo.code_contracts import CodeRouteConfig
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile
from _04_Nucleo_Operativo.code_route import CodeRoute
from _04_Nucleo_Operativo.external_deep_coverage import (
    DeepCoverageExecution,
    DeepCoverageProgress,
)
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderBaseline,
    ExternalProviderStatus,
    ProviderDescriptor,
    ProviderLimits,
)
from _04_Nucleo_Operativo.external_evidence_providers import (
    COSMIC_RAY_MUTATION_PROVIDER_ID,
    PYTEST_COVERAGE_PROVIDER_ID,
    VULTURE_UNUSED_PROVIDER_ID,
    PytestCoverageTrustedDeepProvider,
    provider_tool_versions,
    providers_for_profile,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


def _project(root: Path) -> tuple[Path, Path]:
    package = root / "_04_Nucleo_Operativo"
    tests = root / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    source = package / "sample.py"
    test = tests / "test_sample.py"
    source.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    test.write_text(
        "from _04_Nucleo_Operativo.sample import value\n\n"
        "def test_value() -> None:\n    assert value() == 1\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n[tool.pyright]\n",
        encoding="utf-8",
    )
    return source, test


def _config(root: Path, scratch: Path) -> CodeRouteConfig:
    return CodeRouteConfig(
        state_path=scratch / "code.sqlite3",
        dedup_path=scratch / "dedup.sqlite3",
        external_evidence_root=root,
        analysis_profile="trusted-deep",
        deep_test_selectors=("tests/test_sample.py",),
        deep_max_tests=40,
        deep_time_budget_seconds=90,
        deep_shard_size=5,
    )


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


def test_trusted_deep_registry_extends_static_matrix_with_declared_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    _project(root)
    config = _config(root, scratch)
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

    progress = RecordingProgress()
    providers = providers_for_profile(
        "trusted-deep",
        root,
        deep_configuration=config.deep_configuration_payload,
        deep_configuration_signature=config.deep_configuration_signature,
        progress=progress,
    )

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
        PYTEST_COVERAGE_PROVIDER_ID,
        COSMIC_RAY_MUTATION_PROVIDER_ID,
    )
    descriptor = next(
        item.descriptor
        for item in providers
        if item.descriptor.provider_id == PYTEST_COVERAGE_PROVIDER_ID
    )
    assert descriptor.profile == "trusted-deep"
    assert descriptor.trust_requirement == "trusted-execution"
    assert descriptor.invalidation_strategy == "dynamic_suite"
    assert descriptor.loads_project_configuration is True
    assert descriptor.loads_plugins is True
    assert descriptor.imports_content is True
    assert descriptor.executes_content is True
    assert descriptor.uses_network is True
    assert descriptor.mutation_authority is False
    assert descriptor.limits.timeout_seconds == 90.0
    coverage_provider = next(
        item for item in providers if item.descriptor.provider_id == PYTEST_COVERAGE_PROVIDER_ID
    )
    assert isinstance(coverage_provider, PytestCoverageTrustedDeepProvider)
    coverage_provider._emit_progress(
        DeepCoverageProgress("shard_completed", 2, 3, 1, 120, 1, 45, 12)
    )
    assert progress.events[-1].phase == "trusted-deep-coverage"
    assert progress.events[-1].completed == 2
    assert {metric.name: metric.value for metric in progress.events[-1].metrics} == {
        "status": "shard_completed",
        "tests": 120,
        "reused": 1,
        "elapsed_seconds": 45,
        "shard_seconds": 12,
    }
    vulture_descriptor = next(
        item.descriptor
        for item in providers
        if item.descriptor.provider_id == VULTURE_UNUSED_PROVIDER_ID
    )
    assert vulture_descriptor.profile == "trusted-static"
    assert vulture_descriptor.scope == "current-inventory-python"
    assert vulture_descriptor.invalidation_strategy == "project_wide"
    assert vulture_descriptor.loads_project_configuration is False
    assert vulture_descriptor.loads_plugins is False
    assert vulture_descriptor.imports_content is False
    assert vulture_descriptor.executes_content is False
    assert vulture_descriptor.uses_network is False
    assert vulture_descriptor.mutation_authority is False
    assert provider_tool_versions()[VULTURE_UNUSED_PROVIDER_ID] == "vulture-test"
    assert provider_tool_versions()[PYTEST_COVERAGE_PROVIDER_ID] == (
        "pytest=pytest-test;coverage=coverage-test"
    )


def test_exact_replay_observes_support_and_never_executes_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    source, test = _project(root)
    subprocess.run(("git", "init", "--quiet"), cwd=root, check=True)
    subprocess.run(("git", "add", "."), cwd=root, check=True)
    monkeypatch.setattr(deep_module, "_canonical_repository_root", lambda: root)
    monkeypatch.setattr(providers_module, "_deep_tool_version", lambda: "pytest=9;coverage=7")
    config = _config(root, scratch)
    files = (
        _external_file(source, root, 1),
        _external_file(test, root, 2),
    )
    execution = DeepCoverageExecution(
        (),
        (),
        (),
        11,
        3,
        2,
        "selected",
        True,
        "suite:fixture",
        "scope:fixture",
        {
            "tests_collected": 1,
            "tests_selected": 1,
            "tests_passed": 1,
            "tests_failed": 0,
            "tests_skipped": 0,
            "shards_total": 1,
            "shards_reused": 0,
            "process_invocations": 2,
            "stdout_bytes": 11,
            "stderr_bytes": 3,
            "measurement_complete": 1,
        },
        ("coverage_main_process_only",),
    )
    monkeypatch.setattr(providers_module, "execute_pytest_coverage", lambda *_a, **_k: execution)
    first_provider = PytestCoverageTrustedDeepProvider(
        root,
        config.deep_configuration_payload,
        config.deep_configuration_signature,
    )
    first_signature = first_provider.baseline_input_signature(files)
    first = first_provider.run(root, files, baseline=None, scratch_root=scratch)
    assert first.status == "completed"
    assert first.input_signature == first_signature
    assert first.counters["bytes_staged"] == 0
    assert first.counters["support_files_verified"] == 3
    assert first.counters["process_invocations"] == 2
    assert first.result_digest is not None
    baseline = ExternalProviderBaseline(
        17,
        PYTEST_COVERAGE_PROVIDER_ID,
        first.publication.tool_version,
        first.input_signature,
        first.descriptor.comparability_signature,
        first.result_digest,
        (),
        (),
        (),
        reuse_mode="exact_replay",
    )
    replay_provider = PytestCoverageTrustedDeepProvider(
        root,
        config.deep_configuration_payload,
        config.deep_configuration_signature,
    )
    replay_signature = replay_provider.baseline_input_signature(files)
    monkeypatch.setattr(
        providers_module,
        "execute_pytest_coverage",
        lambda *_a, **_k: pytest.fail("whole-publication replay executed pytest"),
    )

    replay = replay_provider.run(root, files, baseline=baseline, scratch_root=scratch)

    assert replay_signature == first_signature
    assert replay.execution == "cache_replay"
    assert replay.replay_source_tool_run_id == 17
    assert replay.counters["cache_hits"] == 1
    assert replay.counters["process_invocations"] == 1
    assert replay.counters["support_files_verified"] == 3
    assert replay.counters["support_bytes_verified"] > 0
    assert replay.publication.provenance["deep_configuration"] == {
        "payload": config.deep_configuration_payload,
        "signature": config.deep_configuration_signature,
    }
    assert replay.publication.provenance["deep_execution"]["whole_publication_replay"] is True


def test_code_route_passes_exact_deep_payload_to_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    config = _config(root, scratch)
    captured: dict[str, object] = {}

    class Captured(RuntimeError):
        pass

    def capture(profile, observed_root, **kwargs):
        captured.update(profile=profile, root=observed_root, **kwargs)
        raise Captured

    monkeypatch.setattr(code_route_module, "providers_for_profile", capture)
    route = CodeRoute(
        config,
        SimpleNamespace(),
        SimpleNamespace(),
        framework_run_id=1,
        scan_id=1,
    )
    state = SimpleNamespace(external_evidence_files=lambda _root: ())

    with pytest.raises(Captured):
        route._external_evidence(
            state,
            full_reconciliation=True,
            counters={},
        )

    assert captured == {
        "profile": "trusted-deep",
        "root": root,
        "deep_configuration": config.deep_configuration_payload,
        "deep_configuration_signature": config.deep_configuration_signature,
        "progress": None,
    }


def _runtime_descriptor() -> ProviderDescriptor:
    return ProviderDescriptor(
        PYTEST_COVERAGE_PROVIDER_ID,
        "neocortex.pytest-coverage-trusted-deep/v1",
        "pytest+coverage",
        "trusted-deep",
        "trusted-execution",
        "canonical-neocortex-pytest-selection-v1",
        "external:pytest-coverage-trusted-deep",
        "configuration:fixture",
        "project:fixture",
        "environment:fixture",
        "comparability:fixture",
        "bounded-fixture",
        "dynamic_suite",
        "exact-publication-replay-v1",
        ProviderLimits(90.0, 1_000_000, 1_000_000, 1_000_000, 100),
        loads_project_configuration=True,
        loads_plugins=True,
        imports_content=True,
        executes_content=True,
        uses_network=True,
    )


def test_runtime_reconstruction_uses_persisted_deep_configuration_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    config = _config(root, scratch)
    descriptor = _runtime_descriptor()
    captured: dict[str, object] = {}

    def registry(profile, observed_root, **kwargs):
        captured.update(profile=profile, root=observed_root, **kwargs)
        return (SimpleNamespace(descriptor=descriptor, tool_version=lambda: "fixture-1"),)

    monkeypatch.setattr(providers_module, "providers_for_profile", registry)
    row = {
        "profile": "trusted-deep",
        "provenance_json": json.dumps(
            {
                "deep_configuration": {
                    "payload": config.deep_configuration_payload,
                    "signature": config.deep_configuration_signature,
                }
            }
        ),
        "observed_root": str(root),
        "provider_id": PYTEST_COVERAGE_PROVIDER_ID,
        "tool_version": "fixture-1",
        "provider_schema": descriptor.provider_schema,
        "configuration_signature": descriptor.configuration_signature,
        "environment_signature": descriptor.environment_signature,
        "comparability_signature": descriptor.comparability_signature,
    }

    assert store_module._current_runtime_reason(row) is None
    assert captured["deep_configuration"] == config.deep_configuration_payload
    assert captured["deep_configuration_signature"] == config.deep_configuration_signature

    stale = {**row, "tool_version": "fixture-old"}
    assert store_module._current_runtime_reason(stale) == "external_provider_runtime_stale"

    malformed = {**row, "provenance_json": json.dumps({"deep_configuration": {}})}
    assert (
        store_module._current_runtime_reason(malformed)
        == "external_provider_deep_configuration_invalid"
    )


def test_suite_profile_priority_is_deep_over_static_and_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"provider_id": "ruff-protected-basic", "profile": "protected", "status": "completed"},
        {"provider_id": "mypy-trusted-project", "profile": "trusted-static", "status": "completed"},
        {
            "provider_id": PYTEST_COVERAGE_PROVIDER_ID,
            "profile": "trusted-deep",
            "status": "completed",
        },
    ]

    class Connection:
        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(fetchall=lambda: rows)

    def provider_status(_connection, row):
        status = ExternalProviderStatus(
            provider_id=row["provider_id"],
            provider_schema=f"schema:{row['provider_id']}",
            profile=row["profile"],
            tool_name="fixture",
            tool_version="1",
            status="ready",
            reason=None,
            execution="full",
            eligible_files=1,
            covered_files=1,
            findings=0,
            added=None,
            resolved=None,
            comparable=False,
            result_digest="digest",
            comparability_signature="comparison",
            gate="baseline",
            content_executed=row["profile"] == "trusted-deep",
        )
        return status, (), 1, (), ()

    monkeypatch.setattr(store_module, "_provider_status", provider_status)

    suite = store_module.read_external_evidence_suite(
        Connection(),  # type: ignore[arg-type]
        1,
        enforce_current_runtime=False,
    )

    assert suite.profile == "trusted-deep"
    assert suite.status == "ready"
