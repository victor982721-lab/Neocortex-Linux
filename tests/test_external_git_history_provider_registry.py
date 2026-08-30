"""Registry, exact-replay and fail-closed contracts for local Git history."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import neocortex.code.external_evidence_providers as providers_module
from neocortex.code.code_contracts import CodeRouteConfig
from neocortex.code.code_external_evidence import ExternalEvidenceFile
from neocortex.code.external_evidence_models import ExternalProviderBaseline
from neocortex.code.external_evidence_providers import (
    GIT_HISTORY_PROVIDER_ID,
    GitHistoryLocalProvider,
    provider_tool_versions,
    providers_for_profile,
)
from neocortex.code.external_git_history import GitHistoryConfig
from neocortex.semantic.semantic_models import fingerprint_bytes


def _git(root: Path, *arguments: str, environment: dict[str, str] | None = None) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        env=environment,
    )
    return completed.stdout


def _commit(root: Path, message: str, timestamp: str) -> None:
    environment = dict(os.environ)
    environment.update({"GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp})
    _git(
        root,
        "-c",
        "user.name=NeoCortex fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        message,
        environment=environment,
    )


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


def _repository(tmp_path: Path) -> tuple[Path, tuple[ExternalEvidenceFile, ...]]:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--quiet")
    package = root / "pkg"
    package.mkdir()
    (package / "a.py").write_text("a = 1\n", encoding="utf-8")
    (package / "b.py").write_text("b = 1\n", encoding="utf-8")
    _git(root, "add", "--all")
    _commit(root, "initial", "2026-01-01T00:00:00+00:00")
    (package / "a.py").write_text("a = 2\n", encoding="utf-8")
    (package / "b.py").write_text("b = 2\n", encoding="utf-8")
    _git(root, "add", "--all")
    _commit(root, "change together", "2026-01-02T00:00:00+00:00")
    return root, (_owner(root, "pkg/a.py", 1), _owner(root, "pkg/b.py", 2))


def _baseline(publication, tool_run_id: int = 41) -> ExternalProviderBaseline:
    assert publication.result_digest is not None
    return ExternalProviderBaseline(
        tool_run_id,
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


def test_real_provider_full_and_exact_replay_preserve_costs_and_repository(
    tmp_path: Path,
) -> None:
    root, files = _repository(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    provider = GitHistoryLocalProvider(
        root,
        config=GitHistoryConfig(max_commits=10, max_relations=10),
    )
    before_status = _git(root, "status", "--porcelain=v2", "-z")
    before_head = _git(root, "rev-parse", "HEAD")

    first = provider.run(root, files, baseline=None, scratch_root=scratch)

    assert first.status == "completed"
    assert first.execution == "full"
    assert first.descriptor.provider_id == GIT_HISTORY_PROVIDER_ID
    assert first.descriptor.profile == "trusted-static"
    assert first.descriptor.trust_requirement == "trusted-static"
    assert first.descriptor.loads_project_configuration is False
    assert first.descriptor.loads_plugins is False
    assert first.descriptor.imports_content is False
    assert first.descriptor.executes_content is False
    assert first.descriptor.uses_network is False
    assert first.descriptor.mutation_authority is False
    assert first.counters["process_invocations"] == 4
    assert first.counters["stdout_bytes"] > 0
    assert first.counters["wall_milliseconds"] >= 0
    assert first.counters["bytes_staged"] == 0
    assert first.metrics
    assert first.relations
    assert _git(root, "status", "--porcelain=v2", "-z") == before_status == b""
    assert _git(root, "rev-parse", "HEAD") == before_head

    signature = provider.baseline_input_signature(files)
    assert signature == first.input_signature
    provider.executor = lambda *_args, **_kwargs: pytest.fail("exact replay scanned history")
    replay = provider.run(root, files, baseline=_baseline(first), scratch_root=scratch)

    assert replay.status == "skipped"
    assert replay.execution == "cache_replay"
    assert replay.input_signature == first.input_signature
    assert replay.result_digest == first.result_digest
    assert replay.replay_source_tool_run_id == 41
    assert replay.counters["process_invocations"] == 2
    assert replay.counters["stdout_bytes"] > 0
    assert replay.counters["wall_milliseconds"] >= 0
    assert replay.counters["cache_hits"] == 1
    assert _git(root, "status", "--porcelain=v2", "-z") == b""
    assert _git(root, "rev-parse", "HEAD") == before_head


def test_new_head_invalidates_exact_history_replay(tmp_path: Path) -> None:
    root, files = _repository(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    provider = GitHistoryLocalProvider(
        root,
        config=GitHistoryConfig(max_commits=10, max_relations=10),
    )
    first = provider.run(root, files, baseline=None, scratch_root=scratch)

    (root / "release.txt").write_text("new HEAD only\n", encoding="utf-8")
    _git(root, "add", "--all")
    _commit(root, "new head", "2026-01-03T00:00:00+00:00")
    second = provider.run(root, files, baseline=_baseline(first), scratch_root=scratch)

    assert second.status == "completed"
    assert second.execution == "full"
    assert second.input_signature != first.input_signature
    assert second.counters["process_invocations"] == 4
    assert second.counters["cache_hits"] == 0


def test_provider_fails_closed_for_non_repository_and_unavailable_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "not-a-repository"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    source = root / "sample.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    files = (_owner(root, "sample.py", 1),)

    failed = GitHistoryLocalProvider(root).run(
        root,
        files,
        baseline=None,
        scratch_root=scratch,
    )
    assert failed.status == "failed"
    assert failed.counters["errors"] == 1
    assert failed.coverage_complete is False
    assert "Git history root is not the repository top level" in str(failed.publication.provenance)

    monkeypatch.setattr(providers_module, "_git_tool_probe", lambda: (None, None))
    unavailable_provider = GitHistoryLocalProvider(root)
    unavailable = unavailable_provider.run(
        root,
        files,
        baseline=None,
        scratch_root=scratch,
    )
    assert unavailable.status == "unavailable"
    assert unavailable_provider.tool_version() is None
    assert unavailable.counters["process_invocations"] == 0
    assert unavailable.descriptor.uses_network is False


def test_registry_profiles_and_tool_probe_include_local_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n[tool.pyright]\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
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
    static = providers_for_profile("trusted-static", root)
    config = CodeRouteConfig(
        state_path=scratch / "code.sqlite3",
        dedup_path=scratch / "dedup.sqlite3",
        external_evidence_root=root,
        analysis_profile="trusted-deep",
        deep_test_selectors=("tests/test_sample.py",),
        deep_max_tests=10,
        deep_time_budget_seconds=60,
        deep_shard_size=5,
    )
    deep = providers_for_profile(
        "trusted-deep",
        root,
        deep_configuration=config.deep_configuration_payload,
        deep_configuration_signature=config.deep_configuration_signature,
    )

    assert len(static) == 13
    assert len(deep) == 15
    static_git = next(
        item for item in static if item.descriptor.provider_id == GIT_HISTORY_PROVIDER_ID
    )
    deep_git = next(item for item in deep if item.descriptor.provider_id == GIT_HISTORY_PROVIDER_ID)
    assert deep_git.descriptor == static_git.descriptor
    assert static_git.descriptor.profile == "trusted-static"
    assert static_git.descriptor.invalidation_strategy == "project_wide"
    assert static_git.descriptor.execution_strategy == "bounded-local-git-history-v2"
    assert provider_tool_versions()[GIT_HISTORY_PROVIDER_ID] == "2.fixture"
