"""Real-repository and fail-closed tests for local Git history evidence."""

from __future__ import annotations

import inspect
import os
import subprocess
from collections.abc import Callable
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import cast

import pytest

import _04_Nucleo_Operativo.external_git_history as history
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


def _git(repo: Path, *arguments: str, environment: dict[str, str] | None = None) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(repo), *arguments),
        check=True,
        capture_output=True,
        env=environment,
    )
    return completed.stdout


def _commit(repo: Path, message: str, timestamp: str) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
    )
    _git(
        repo,
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
    raw = path.read_bytes()
    metadata = path.stat()
    digest = fingerprint_bytes(raw)
    return ExternalEvidenceFile(
        version_id,
        str(path),
        relative_path,
        metadata.st_mtime_ns,
        len(raw),
        digest.xxh3_128,
        digest.xxh3_64_guard,
    )


def _trace_history_phase(
    monkeypatch: pytest.MonkeyPatch,
    trace: list[str],
    name: str,
) -> None:
    original = cast(Callable[..., object], getattr(history, name))

    def traced(*args: object, **kwargs: object) -> object:
        trace.append(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(history, name, traced)


def _log_header(
    object_id: bytes,
    *,
    timestamp: bytes = b"1700000000",
    parents: bytes = b"",
) -> bytes:
    return (
        b"\x00NEOCORTEX-GIT-HISTORY-COMMIT-V1\x00"
        + object_id
        + b"\x00"
        + timestamp
        + b"\x00"
        + parents
        + b"\x00\x00"
    )


def _repository(tmp_path: Path) -> tuple[Path, tuple[ExternalEvidenceFile, ...]]:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--quiet")
    package = root / "pkg"
    package.mkdir()
    (package / "a.py").write_text("\n".join(f"a_{item} = {item}" for item in range(20)))
    (package / "b.py").write_text("b = 1\n")
    (package / "blob.py").write_bytes(b"\x00binary-one")
    _git(root, "add", "--all")
    _commit(root, "initial", "2026-01-01T00:00:00+00:00")

    (package / "a.py").write_text(
        "\n".join(f"a_{item} = {item}" for item in range(20)) + "\na_more = 20\n"
    )
    (package / "b.py").write_text("b = 1\nb_more = 2\n")
    (package / "blob.py").write_bytes(b"\x00binary-two")
    _git(root, "add", "--all")
    _commit(root, "change together", "2026-01-11T00:00:00+00:00")

    _git(root, "mv", "pkg/a.py", "pkg/c.py")
    _git(root, "add", "--all")
    _commit(root, "rename", "2026-02-01T00:00:00+00:00")
    return root, (
        _owner(root, "pkg/b.py", 1),
        _owner(root, "pkg/blob.py", 2),
        _owner(root, "pkg/c.py", 3),
    )


def test_real_repository_reports_bounded_history_renames_and_cochange(
    tmp_path: Path,
) -> None:
    root, files = _repository(tmp_path)
    before_status = _git(root, "status", "--porcelain=v2", "-z")
    before_head = _git(root, "rev-parse", "HEAD")
    config = history.GitHistoryConfig(max_commits=10, max_relations=10)

    result = history.execute_git_history(root, files, os.environ, config=config)

    after_status = _git(root, "status", "--porcelain=v2", "-z")
    after_head = _git(root, "rev-parse", "HEAD")
    values = {
        (item.subject_kind, item.subject_key, item.metric_name): item.value
        for item in result.metrics
    }
    relations = {(item.source_key, item.target_key): item for item in result.relations}

    assert before_status == after_status == b""
    assert before_head == after_head
    assert result.head_commit == before_head.decode().strip()
    assert result.requested_ref == "HEAD"
    assert result.history_truncated is False
    assert result.repository_shallow is False
    assert result.findings == ()
    assert result.process_invocations == 4
    assert result.counters["wall_milliseconds"] >= 0
    assert result.counters["commits_observed"] == 3
    assert result.counters["rename_entries"] == 1
    assert result.counters["binary_or_unmeasured_entries"] == 2
    assert values[("file", "pkg/c.py", "observed_commit_count")] == 3
    assert values[("file", "pkg/b.py", "observed_commit_count")] == 2
    assert values[("file", "pkg/blob.py", "binary_or_unmeasured_touch_count")] == 2
    assert values[("module", "pkg.c", "observed_commit_count")] == 3
    assert values[("file", "pkg/c.py", "observed_age_seconds")] == 31 * 24 * 60 * 60
    assert values[("file", "pkg/b.py", "observed_recency_seconds")] == 21 * 24 * 60 * 60
    assert values[("file", "pkg/c.py", "observed_change_frequency_per_100_commits")] == 100
    assert relations[("pkg/b.py", "pkg/c.py")].metadata["observed_commits_together"] == 2
    assert relations[("pkg/b.py", "pkg/c.py")].directed is False
    assert relations[("pkg/b.py", "pkg/c.py")].confidence is None
    assert "not_defect_probability" in str(
        relations[("pkg/b.py", "pkg/c.py")].metadata["interpretation"]
    )
    assert result.provenance["uses_network"] is False
    assert result.provenance["executes_content"] is False
    assert result.provenance["mutation_authority"] is False


def test_exact_signature_binds_head_inventory_and_configuration(tmp_path: Path) -> None:
    root, files = _repository(tmp_path)
    first_config = history.GitHistoryConfig(max_commits=10)
    snapshot = history.inspect_git_repository(root, os.environ, config=first_config)
    first = history.git_history_input_signature(files, snapshot, config=first_config)
    assert first == history.git_history_input_signature(files, snapshot, config=first_config)

    second_config = history.GitHistoryConfig(max_commits=2)
    assert first != history.git_history_input_signature(files, snapshot, config=second_config)

    (root / "pkg" / "d.py").write_text("d = 1\n")
    _git(root, "add", "--all")
    _commit(root, "new head", "2026-02-02T00:00:00+00:00")
    second_snapshot = history.inspect_git_repository(root, os.environ, config=first_config)
    assert snapshot.head_commit != second_snapshot.head_commit
    assert first != history.git_history_input_signature(files, second_snapshot, config=first_config)


def test_truncated_window_marks_absence_without_inventing_age(tmp_path: Path) -> None:
    root, files = _repository(tmp_path)
    result = history.execute_git_history(
        root,
        files,
        os.environ,
        config=history.GitHistoryConfig(max_commits=1, max_relations=10),
    )
    values = {
        (item.subject_key, item.metric_name): item.value
        for item in result.metrics
        if item.subject_kind == "file"
    }

    assert result.history_truncated is True
    assert result.counters["commits_observed"] == 1
    assert result.counters["covered_files"] == result.counters["eligible_files"]
    assert result.counters["files_without_observed_history"] == 2
    assert values[("pkg/b.py", "history_observed")] == 0
    assert ("pkg/b.py", "observed_age_seconds") not in values
    assert "commit_window_truncated" in result.limitations
    assert "some_current_files_absent_from_observed_history" in result.limitations


def test_shallow_repository_is_distinct_from_commit_window_truncation(tmp_path: Path) -> None:
    source, _files = _repository(tmp_path)
    clone = tmp_path / "shallow"
    subprocess.run(
        ("git", "clone", "--quiet", "--depth", "1", source.as_uri(), str(clone)),
        check=True,
        capture_output=True,
    )
    files = (
        _owner(clone, "pkg/b.py", 1),
        _owner(clone, "pkg/blob.py", 2),
        _owner(clone, "pkg/c.py", 3),
    )

    result = history.execute_git_history(
        clone,
        files,
        os.environ,
        config=history.GitHistoryConfig(max_commits=10, max_relations=10),
    )

    assert result.repository_shallow is True
    assert result.history_truncated is False
    assert result.counters["repository_shallow"] == 1
    assert result.counters["history_truncated"] == 0
    assert "shallow_repository_history_incomplete" in result.limitations


def test_parser_rejects_path_escape_and_excess_changes() -> None:
    header = b"\x00NEOCORTEX-GIT-HISTORY-COMMIT-V1\x00" + b"a" * 40 + b"\x001700000000\x00\x00\x00"
    with pytest.raises(ValueError, match="escapes"):
        history._parse_git_log(header + b"1\t0\t../escape.py\x00", max_change_entries=10)
    with pytest.raises(ValueError, match="change-entry bound"):
        history._parse_git_log(header + b"1\t0\ta.py\x001\t0\tb.py\x00", max_change_entries=1)


def test_git_log_parser_signature_and_exact_edge_semantics_are_frozen() -> None:
    assert str(inspect.signature(history._parse_git_log)) == (
        "(raw: 'bytes', *, max_change_entries: 'int') -> 'tuple[_GitCommit, ...]'"
    )
    first_id = b"a" * 40
    second_id = b"b" * 64
    raw = (
        _log_header(first_id)
        + b"\n2\t1\tpkg/a.py\x00"
        + b"-\t-\tpkg/blob.bin\x00"
        + b"3\t4\t\x00pkg/old.py\x00pkg/new.py\x00"
        + _log_header(second_id, timestamp=b"1700000100", parents=first_id)
        + b"\n"
    )
    expected = (
        history._GitCommit(
            "a" * 40,
            1_700_000_000,
            (),
            (
                history._GitChange("pkg/a.py", 2, 1),
                history._GitChange("pkg/blob.bin", None, None),
                history._GitChange("pkg/new.py", 3, 4, "pkg/old.py"),
            ),
        ),
        history._GitCommit("b" * 64, 1_700_000_100, ("a" * 40,), ()),
    )

    assert history._parse_git_log(raw, max_change_entries=3) == expected
    assert history._parse_git_log(raw, max_change_entries=3) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    (
        (b"not-delimited", "initial delimiter"),
        (b"\x00", "did not return any non-merge commit"),
        (_log_header(b"g" * 40) + b"\n", "identity is invalid"),
        (_log_header(b"a" * 40, timestamp=b"now") + b"\n", "timestamp is invalid"),
        (
            _log_header(b"a" * 40, parents=b"b" * 40 + b" " + b"c" * 40)
            + b"\n",
            "merge exclusion contract",
        ),
        (_log_header(b"a" * 40) + b"1\t-\tpkg/a.py\x00", "binary markers disagree"),
        (_log_header(b"a" * 40) + b"one\t0\tpkg/a.py\x00", "line delta is invalid"),
        (_log_header(b"a" * 40) + b"1\t0\tpkg/a.py", "record is unterminated"),
    ),
)
def test_git_log_parser_representative_fail_closed_errors(
    raw: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        history._parse_git_log(raw, max_change_entries=10)


def test_ref_and_repository_root_contracts_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="revision expression"):
        history.GitHistoryConfig(ref="--all")
    root, _files = _repository(tmp_path)
    with pytest.raises(ValueError, match="top level"):
        history.inspect_git_repository(root / "pkg", os.environ)


def test_git_invocation_scopes_safe_directory_to_exact_validated_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    aliased_root = root / ".." / root.name
    resolved_root = str(root.resolve())

    assert history._git_prefix("git", aliased_root) == (
        "git",
        "--no-pager",
        "--no-optional-locks",
        "--no-replace-objects",
        "-c",
        f"safe.directory={resolved_root}",
        "-C",
        resolved_root,
    )
    assert history.GitHistoryConfig().as_payload()["safe_directory_policy"] == (
        "command-scoped-exact-validated-root-v1"
    )


def test_git_environment_rejects_inherited_configuration_injection() -> None:
    controlled = history._git_environment(
        {
            "PATH": "fixture-path",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        }
    )

    assert controlled["PATH"] == "fixture-path"
    assert controlled["GIT_CONFIG_NOSYSTEM"] == "1"
    assert controlled["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GIT_CONFIG_COUNT" not in controlled
    assert "GIT_CONFIG_KEY_0" not in controlled
    assert "GIT_CONFIG_VALUE_0" not in controlled


def test_execute_git_history_public_contract_and_phase_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert str(inspect.signature(history.execute_git_history)) == (
        "(root: 'Path', files: 'Sequence[ExternalEvidenceFile]', "
        "environment: 'Mapping[str, str]', *, config: 'GitHistoryConfig | None' = "
        "None, snapshot: 'GitRepositorySnapshot | None' = None, "
        "git_executable: 'str' = 'git') -> 'GitHistoryExecution'"
    )
    root, files = _repository(tmp_path)
    trace: list[str] = []
    ordered_phases = (
        "_collect_git_history_window",
        "_observations",
        "_git_history_shared_metadata",
        "_git_history_metrics",
        "_git_history_relations",
        "_git_history_limitations",
        "_git_history_counters",
        "_git_history_execution",
        "_git_history_provenance",
    )
    for phase in ordered_phases:
        _trace_history_phase(monkeypatch, trace, phase)

    result = history.execute_git_history(
        root,
        files,
        os.environ,
        config=history.GitHistoryConfig(max_commits=10, max_relations=10),
    )

    assert tuple(trace) == ordered_phases
    assert tuple(item.name for item in dataclass_fields(result)) == (
        "findings",
        "metrics",
        "relations",
        "history_input_signature",
        "configuration_signature",
        "requested_ref",
        "head_commit",
        "repository_shallow",
        "history_truncated",
        "relations_truncated",
        "counters",
        "limitations",
        "provenance",
        "stdout_bytes",
        "stderr_bytes",
        "process_invocations",
    )
    assert result.process_invocations == 4
    assert result.counters["metrics_emitted"] == len(result.metrics)
    assert result.counters["relations_emitted"] == len(result.relations)


def test_execute_git_history_propagates_cancellation_before_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, files = _repository(tmp_path)
    before_status = _git(root, "status", "--porcelain=v2", "-z")
    projection_started = False

    def cancel_observation(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt("injected Git history cancellation")

    def reject_projection(*_args: object, **_kwargs: object) -> object:
        nonlocal projection_started
        projection_started = True
        raise AssertionError("projection started after cancellation")

    monkeypatch.setattr(history, "_observations", cancel_observation)
    monkeypatch.setattr(history, "_git_history_metrics", reject_projection)

    with pytest.raises(KeyboardInterrupt, match="injected Git history cancellation"):
        history.execute_git_history(
            root,
            files,
            os.environ,
            config=history.GitHistoryConfig(max_commits=10, max_relations=10),
        )

    assert projection_started is False
    assert _git(root, "status", "--porcelain=v2", "-z") == before_status == b""


def test_history_observations_preserve_rename_ambiguity_and_cochange_bound(
    tmp_path: Path,
) -> None:
    _root, files = _repository(tmp_path)
    owners = {item.relative_path: item for item in files}
    ambiguous = history._GitCommit(
        "a" * 40,
        1_700_000_000,
        (),
        (
            history._GitChange("pkg/b.py", 1, 0, "pkg/old.py"),
            history._GitChange("pkg/c.py", 1, 0, "pkg/old.py"),
        ),
    )
    with pytest.raises(ValueError, match="rename lineage is ambiguous"):
        history._observations((ambiguous,), owners, history.GitHistoryConfig())

    large_commit = history._GitCommit(
        "b" * 40,
        1_700_000_100,
        (),
        (
            history._GitChange("pkg/b.py", 1, 0),
            history._GitChange("pkg/c.py", 1, 0),
        ),
    )
    aggregates, cochanges, counters = history._observations(
        (large_commit,),
        owners,
        history.GitHistoryConfig(max_files_per_cochange_commit=1),
    )

    assert set(aggregates) == set(owners)
    assert cochanges == {}
    assert counters == {
        "change_entries": 2,
        "rename_entries": 0,
        "binary_or_unmeasured_entries": 0,
        "commits_touching_current_files": 1,
        "cochange_commits_skipped_large": 1,
    }
