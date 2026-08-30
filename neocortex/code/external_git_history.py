"""Bounded, read-only Git history evidence for the current code inventory.

The adapter deliberately reports observations, not defect probabilities.  It
reads committed objects from one verified local repository, follows renames
within a bounded newest-to-oldest window, and never invokes hooks, textconv,
external diff drivers, a pager, or the network.
"""

from __future__ import annotations
import os
import re
import subprocess
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from neocortex.runtime.control.bounded_subprocess import run_bounded_capture
from .code_external_evidence import ExternalEvidenceFile, external_input_signature
from .external_evidence_models import (
    ExternalProviderFinding,
    ExternalProviderMetric,
    ExternalProviderRelation,
    ExternalSubjectKind,
    external_metric_identity,
    external_relation_identity,
    external_signature,
)

GIT_HISTORY_PROVIDER_ID = "git-history-local"
GIT_HISTORY_PROVIDER_SCHEMA = "neocortex.git-history-local/v1"

_LOG_MARKER = b"NEOCORTEX-GIT-HISTORY-COMMIT-V1"
_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@{}^~:+-]{0,255}")
_MAX_PATH_BYTES = 4_096
_MAX_LINE_DELTA = 1_000_000_000_000
_MEMORY_BOUND_BYTES = 512 * 1024 * 1024
_SECONDS_PER_100_COMMITS = 100.0
_SAFE_DIRECTORY_POLICY = "command-scoped-exact-validated-root-v1"


@dataclass(frozen=True, slots=True)
class GitHistoryConfig:
    """Versioned hard bounds and semantics for one local history observation."""

    ref: str = "HEAD"
    max_commits: int = 2_000
    max_files: int = 2_000
    max_change_entries: int = 100_000
    max_files_per_cochange_commit: int = 64
    max_relations: int = 10_000
    timeout_seconds: float = 120.0
    stdout_limit_bytes: int = 16 * 1024 * 1024
    stderr_limit_bytes: int = 128 * 1024

    def __post_init__(self) -> None:
        if _REF_PATTERN.fullmatch(self.ref) is None or ".." in self.ref:
            raise ValueError("Git history ref is not a bounded revision expression")
        for name in (
            "max_commits",
            "max_files",
            "max_change_entries",
            "max_files_per_cochange_commit",
            "max_relations",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Git history {name} must be a positive integer")
        if self.max_commits > 100_000:
            raise ValueError("Git history commit bound is excessive")
        if self.max_files > 100_000 or self.max_change_entries > 2_000_000:
            raise ValueError("Git history input bound is excessive")
        if self.max_files_per_cochange_commit > 1_000 or self.max_relations > 100_000:
            raise ValueError("Git history relation bound is excessive")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 900:
            raise ValueError("Git history timeout is outside the supported range")

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": "neocortex.git-history-config/v1",
            "ref": self.ref,
            "max_commits": self.max_commits,
            "max_files": self.max_files,
            "max_change_entries": self.max_change_entries,
            "max_files_per_cochange_commit": self.max_files_per_cochange_commit,
            "max_relations": self.max_relations,
            "timeout_seconds": self.timeout_seconds,
            "stdout_limit_bytes": self.stdout_limit_bytes,
            "stderr_limit_bytes": self.stderr_limit_bytes,
            "merge_policy": "exclude-merges-v1",
            "rename_policy": "find-renames-50-percent-window-v1",
            "recency_reference": "maximum-observed-commit-timestamp-v1",
            "cochange_policy": "current-files-same-nonmerge-commit-v1",
            "safe_directory_policy": _SAFE_DIRECTORY_POLICY,
            "network": False,
            "content_execution": False,
            "mutation_authority": False,
        }

    @property
    def signature(self) -> str:
        return external_signature("git-history-configuration-v1", self.as_payload())


@dataclass(frozen=True, slots=True)
class GitRepositorySnapshot:
    requested_ref: str
    head_commit: str
    repository_shallow: bool
    process_invocations: int
    stdout_bytes: int
    stderr_bytes: int


@dataclass(frozen=True, slots=True)
class GitHistoryExecution:
    findings: tuple[ExternalProviderFinding, ...]
    metrics: tuple[ExternalProviderMetric, ...]
    relations: tuple[ExternalProviderRelation, ...]
    history_input_signature: str
    configuration_signature: str
    requested_ref: str
    head_commit: str
    repository_shallow: bool
    history_truncated: bool
    relations_truncated: bool
    counters: Mapping[str, int]
    limitations: tuple[str, ...]
    provenance: Mapping[str, object]
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int


@dataclass(frozen=True, slots=True)
class _GitChange:
    path: str
    additions: int | None
    deletions: int | None
    renamed_from: str | None = None


@dataclass(frozen=True, slots=True)
class _GitCommit:
    object_id: str
    timestamp: int
    parents: tuple[str, ...]
    changes: tuple[_GitChange, ...]


@dataclass(slots=True)
class _HistoryAggregate:
    commits: set[str] = field(default_factory=set)
    touches: int = 0
    additions: int = 0
    deletions: int = 0
    binary_or_unmeasured_touches: int = 0
    first_timestamp: int | None = None
    last_timestamp: int | None = None

    def record(self, commit: _GitCommit, change: _GitChange) -> None:
        self.commits.add(commit.object_id)
        self.touches += 1
        if change.additions is None or change.deletions is None:
            self.binary_or_unmeasured_touches += 1
        else:
            self.additions += change.additions
            self.deletions += change.deletions
        if self.first_timestamp is None or commit.timestamp < self.first_timestamp:
            self.first_timestamp = commit.timestamp
        if self.last_timestamp is None or commit.timestamp > self.last_timestamp:
            self.last_timestamp = commit.timestamp

    def absorb(self, other: _HistoryAggregate) -> None:
        self.commits.update(other.commits)
        self.touches += other.touches
        self.additions += other.additions
        self.deletions += other.deletions
        self.binary_or_unmeasured_touches += other.binary_or_unmeasured_touches
        if other.first_timestamp is not None and (
            self.first_timestamp is None or other.first_timestamp < self.first_timestamp
        ):
            self.first_timestamp = other.first_timestamp
        if other.last_timestamp is not None and (
            self.last_timestamp is None or other.last_timestamp > self.last_timestamp
        ):
            self.last_timestamp = other.last_timestamp


def _validated_root(root: Path) -> Path:
    if "\n" in os.fspath(root) or "\r" in os.fspath(root) or "\x00" in os.fspath(root):
        raise ValueError("Git history root contains an unsupported character")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Git history root cannot be resolved") from exc
    if not resolved.is_dir():
        raise ValueError("Git history root is not a directory")
    return resolved


def _git_environment(environment: Mapping[str, str]) -> dict[str, str]:
    result = {
        key: value
        for key, value in environment.items()
        if not key.upper().startswith("GIT_CONFIG_")
    }
    result.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "PAGER": "cat",
        }
    )
    return result


def _git_prefix(git_executable: str, root: Path) -> tuple[str, ...]:
    if not git_executable or "\x00" in git_executable:
        raise ValueError("Git executable is invalid")
    resolved_root = _validated_root(root)
    # Git evaluates ownership before repository queries.  Authorize only the
    # already validated root, in this process, without persistent configuration.
    return (
        git_executable,
        "--no-pager",
        "--no-optional-locks",
        "--no-replace-objects",
        "-c",
        f"safe.directory={resolved_root}",
        "-C",
        str(resolved_root),
    )


def _unexpected_exit(label: str, completed: subprocess.CompletedProcess[bytes]) -> ValueError:
    raw = completed.stderr or completed.stdout
    detail = " ".join(raw.decode("utf-8", errors="replace").split())[:2_048]
    prefix = f"git_history_{label}_unexpected_exit:{completed.returncode}"
    return ValueError(prefix if not detail else f"{prefix}:{detail}")


def _run_git(
    arguments: Sequence[str],
    *,
    root: Path,
    environment: Mapping[str, str],
    config: GitHistoryConfig,
    git_executable: str,
) -> subprocess.CompletedProcess[bytes]:
    completed = run_bounded_capture(
        (*_git_prefix(git_executable, root), *arguments),
        timeout_seconds=config.timeout_seconds,
        stdout_limit_bytes=config.stdout_limit_bytes,
        stderr_limit_bytes=config.stderr_limit_bytes,
        cwd=root,
        environment=_git_environment(environment),
        memory_limit_bytes=_MEMORY_BOUND_BYTES if os.name == "nt" else None,
    )
    if completed.returncode != 0:
        raise _unexpected_exit(arguments[0], completed)
    return completed


def _parse_root_and_head(raw: bytes, expected_root: Path) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Git root/HEAD output is not UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != 2 or not lines[0] or _OBJECT_ID_PATTERN.fullmatch(lines[1]) is None:
        raise ValueError("Git root/HEAD output has an incompatible schema")
    observed_root = Path(lines[0]).resolve(strict=False)
    if os.path.normcase(os.path.abspath(observed_root)) != os.path.normcase(
        os.path.abspath(expected_root)
    ):
        raise ValueError("Git history root is not the repository top level")
    return lines[1]


def _resolve_root_and_head(
    root: Path,
    environment: Mapping[str, str],
    config: GitHistoryConfig,
    git_executable: str,
) -> tuple[str, int, int]:
    if not (root / ".git").exists():
        raise ValueError("Git history root is not the repository top level")
    completed = _run_git(
        (
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--verify",
            "--end-of-options",
            f"{config.ref}^{{commit}}",
        ),
        root=root,
        environment=environment,
        config=config,
        git_executable=git_executable,
    )
    return (
        _parse_root_and_head(completed.stdout, root),
        len(completed.stdout),
        len(completed.stderr),
    )


def inspect_git_repository(
    root: Path,
    environment: Mapping[str, str],
    *,
    config: GitHistoryConfig | None = None,
    git_executable: str = "git",
) -> GitRepositorySnapshot:
    """Resolve the exact local ref and shallow state without scanning history."""

    effective = GitHistoryConfig() if config is None else config
    resolved_root = _validated_root(root)
    head, root_stdout, root_stderr = _resolve_root_and_head(
        resolved_root, environment, effective, git_executable
    )
    shallow_run = _run_git(
        ("rev-parse", "--is-shallow-repository"),
        root=resolved_root,
        environment=environment,
        config=effective,
        git_executable=git_executable,
    )
    shallow_raw = shallow_run.stdout.strip()
    if shallow_raw not in {b"true", b"false"}:
        raise ValueError("Git shallow-repository output has an incompatible schema")
    return GitRepositorySnapshot(
        effective.ref,
        head,
        shallow_raw == b"true",
        2,
        root_stdout + len(shallow_run.stdout),
        root_stderr + len(shallow_run.stderr),
    )


def _canonical_git_path(raw: bytes) -> str:
    if not raw or len(raw) > _MAX_PATH_BYTES:
        raise ValueError("Git history path is empty or exceeds its byte bound")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Git history path is not UTF-8") from exc
    if "\\" in value:
        raise ValueError("Git history path is not canonical POSIX form")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise ValueError("Git history path escapes its repository")
    return value


def _line_delta(raw: bytes) -> int | None:
    if raw == b"-":
        return None
    if not raw or not raw.isdigit():
        raise ValueError("Git numstat line delta is invalid")
    value = int(raw)
    if value > _MAX_LINE_DELTA:
        raise ValueError("Git numstat line delta exceeds its bound")
    return value


def _parse_change(tokens: list[bytes], index: int) -> tuple[_GitChange, int]:
    pieces = tokens[index].split(b"\t", 2)
    if len(pieces) != 3:
        raise ValueError("Git numstat record has an incompatible schema")
    additions = _line_delta(pieces[0])
    deletions = _line_delta(pieces[1])
    if (additions is None) != (deletions is None):
        raise ValueError("Git numstat binary markers disagree")
    if pieces[2]:
        return _GitChange(_canonical_git_path(pieces[2]), additions, deletions), index + 1
    if index + 2 >= len(tokens) or not tokens[index + 1] or not tokens[index + 2]:
        raise ValueError("Git numstat rename record is incomplete")
    renamed_from = _canonical_git_path(tokens[index + 1])
    path = _canonical_git_path(tokens[index + 2])
    if renamed_from == path:
        raise ValueError("Git numstat rename has identical endpoints")
    return _GitChange(path, additions, deletions, renamed_from), index + 3


def _parse_commit_header(
    tokens: list[bytes],
    index: int,
    seen_commits: set[str],
) -> tuple[str, int, tuple[str, ...], int]:
    if tokens[index] != _LOG_MARKER or index + 5 >= len(tokens):
        raise ValueError("Git history commit record has an incompatible schema")
    try:
        object_id = tokens[index + 1].decode("ascii")
        raw_timestamp = tokens[index + 2].decode("ascii")
        raw_parents = tokens[index + 3].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Git history commit metadata is not ASCII") from exc
    if _OBJECT_ID_PATTERN.fullmatch(object_id) is None or object_id in seen_commits:
        raise ValueError("Git history commit identity is invalid or duplicated")
    if not raw_timestamp.isdigit():
        raise ValueError("Git history commit timestamp is invalid")
    parents = tuple(raw_parents.split()) if raw_parents else ()
    if len(parents) > 1 or any(
        _OBJECT_ID_PATTERN.fullmatch(item) is None for item in parents
    ):
        raise ValueError("Git history merge exclusion contract was violated")
    if tokens[index + 4] != b"":
        raise ValueError("Git history commit header delimiter is invalid")
    return object_id, int(raw_timestamp), parents, index + 5


def _start_commit_changes(tokens: list[bytes], index: int) -> tuple[int, bool]:
    """Consume Git's single header/diff LF and identify an empty commit."""

    if index < len(tokens) and tokens[index].startswith(b"\n"):
        tokens[index] = tokens[index][1:]
        if not tokens[index]:
            return index + 1, True
    return index, False


def _parse_commit_changes(
    tokens: list[bytes],
    index: int,
    change_entries: int,
    max_change_entries: int,
) -> tuple[tuple[_GitChange, ...], int, int]:
    index, empty_commit = _start_commit_changes(tokens, index)
    if empty_commit:
        return (), index, change_entries
    changes: list[_GitChange] = []
    while index < len(tokens) and tokens[index] != b"":
        change, index = _parse_change(tokens, index)
        changes.append(change)
        change_entries += 1
        if change_entries > max_change_entries:
            raise ValueError("Git history change-entry bound was exceeded")
    if index >= len(tokens) or tokens[index] != b"":
        raise ValueError("Git history commit record is unterminated")
    return tuple(changes), index + 1, change_entries


def _parse_git_log(raw: bytes, *, max_change_entries: int) -> tuple[_GitCommit, ...]:
    tokens = raw.split(b"\x00")
    if not tokens or tokens[0] != b"":
        raise ValueError("Git history log is missing its initial delimiter")
    index = 1
    commits: list[_GitCommit] = []
    seen_commits: set[str] = set()
    change_entries = 0
    while index < len(tokens):
        if index == len(tokens) - 1 and tokens[index] == b"":
            break
        object_id, timestamp, parents, index = _parse_commit_header(
            tokens,
            index,
            seen_commits,
        )
        changes, index, change_entries = _parse_commit_changes(
            tokens,
            index,
            change_entries,
            max_change_entries,
        )
        seen_commits.add(object_id)
        commits.append(_GitCommit(object_id, timestamp, parents, changes))
    if not commits:
        raise ValueError("Git history did not return any non-merge commit")
    return tuple(commits)


def _owners_by_relative(
    files: Sequence[ExternalEvidenceFile], config: GitHistoryConfig
) -> dict[str, ExternalEvidenceFile]:
    if len(files) > config.max_files:
        raise ValueError("Git history current-file bound was exceeded")
    result: dict[str, ExternalEvidenceFile] = {}
    folded: set[str] = set()
    for owner in files:
        encoded = owner.relative_path.encode("utf-8")
        path = _canonical_git_path(encoded)
        if path in result or path.casefold() in folded:
            raise ValueError("Git history current inventory has a path collision")
        folded.add(path.casefold())
        result[path] = owner
    return result


def git_history_input_signature(
    files: Sequence[ExternalEvidenceFile],
    snapshot: GitRepositorySnapshot,
    *,
    config: GitHistoryConfig | None = None,
) -> str:
    """Bind generic replay to the exact inventory, ref, HEAD and shallow state."""

    effective = GitHistoryConfig() if config is None else config
    if snapshot.requested_ref != effective.ref:
        raise ValueError("Git history snapshot and configuration refs disagree")
    _owners_by_relative(files, effective)
    return external_signature(
        "git-history-input-v1",
        {
            "inventory_signature": external_input_signature(files),
            "requested_ref": snapshot.requested_ref,
            "head_commit": snapshot.head_commit,
            "repository_shallow": snapshot.repository_shallow,
            "configuration_signature": effective.signature,
        },
    )


def _module_from_path(relative_path: str) -> str | None:
    path = PurePosixPath(relative_path)
    if path.suffix.casefold() not in {".py", ".pyi"}:
        return None
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or None


def _metric(
    *,
    subject_kind: ExternalSubjectKind,
    subject_key: str,
    metric_name: str,
    value: float,
    unit: str,
    version_id: int | None,
    metadata: Mapping[str, object],
) -> ExternalProviderMetric:
    if subject_kind not in {"file", "module"}:
        raise ValueError("Git history metric subject is invalid")
    return ExternalProviderMetric(
        external_metric_identity(
            GIT_HISTORY_PROVIDER_ID,
            subject_kind=subject_kind,
            subject_key=subject_key,
            category="history",
            metric_name=metric_name,
            unit=unit,
        ),
        subject_kind,
        subject_key,
        "history",
        metric_name,
        value,
        unit,
        version_id=version_id,
        metadata=metadata,
    )


def _metrics_for_subject(
    *,
    subject_kind: ExternalSubjectKind,
    subject_key: str,
    version_id: int | None,
    aggregate: _HistoryAggregate,
    window_commits: int,
    reference_timestamp: int,
    shared_metadata: Mapping[str, object],
) -> tuple[ExternalProviderMetric, ...]:
    observed = bool(aggregate.commits)
    metadata = {**shared_metadata, "history_observed": observed}
    definitions: list[tuple[str, float, str]] = [
        ("history_observed", float(observed), "flag"),
        ("observed_commit_count", float(len(aggregate.commits)), "count"),
        ("observed_touch_count", float(aggregate.touches), "count"),
        ("observed_additions", float(aggregate.additions), "lines"),
        ("observed_deletions", float(aggregate.deletions), "lines"),
        (
            "observed_churn_lines",
            float(aggregate.additions + aggregate.deletions),
            "lines",
        ),
        (
            "binary_or_unmeasured_touch_count",
            float(aggregate.binary_or_unmeasured_touches),
            "count",
        ),
        (
            "observed_change_frequency_per_100_commits",
            len(aggregate.commits) * _SECONDS_PER_100_COMMITS / window_commits,
            "changes_per_100_commits",
        ),
    ]
    if aggregate.first_timestamp is not None and aggregate.last_timestamp is not None:
        definitions.extend(
            (
                (
                    "observed_age_seconds",
                    float(max(0, reference_timestamp - aggregate.first_timestamp)),
                    "seconds",
                ),
                (
                    "observed_recency_seconds",
                    float(max(0, reference_timestamp - aggregate.last_timestamp)),
                    "seconds",
                ),
            )
        )
        metadata = {
            **metadata,
            "first_observed_timestamp": aggregate.first_timestamp,
            "last_observed_timestamp": aggregate.last_timestamp,
        }
    return tuple(
        _metric(
            subject_kind=subject_kind,
            subject_key=subject_key,
            metric_name=name,
            value=value,
            unit=unit,
            version_id=version_id,
            metadata=metadata,
        )
        for name, value, unit in definitions
    )


@dataclass(slots=True)
class _HistoryObservationState:
    aggregates: dict[str, _HistoryAggregate]
    aliases: dict[str, str]
    cochanges: defaultdict[tuple[str, str], int]
    counters: dict[str, int]


def _new_history_observation_state(
    owners: Mapping[str, ExternalEvidenceFile],
) -> _HistoryObservationState:
    return _HistoryObservationState(
        aggregates={path: _HistoryAggregate() for path in owners},
        aliases={path: path for path in owners},
        cochanges=defaultdict(int),
        counters={
            "change_entries": 0,
            "rename_entries": 0,
            "binary_or_unmeasured_entries": 0,
            "commits_touching_current_files": 0,
            "cochange_commits_skipped_large": 0,
        },
    )


def _record_history_change(
    state: _HistoryObservationState,
    commit: _GitCommit,
    change: _GitChange,
    touched: set[str],
    pending_aliases: list[tuple[str, str]],
) -> None:
    state.counters["change_entries"] += 1
    if change.renamed_from is not None:
        state.counters["rename_entries"] += 1
    if change.additions is None:
        state.counters["binary_or_unmeasured_entries"] += 1
    current = state.aliases.get(change.path)
    if current is None:
        return
    state.aggregates[current].record(commit, change)
    touched.add(current)
    if change.renamed_from is not None:
        pending_aliases.append((change.renamed_from, current))


def _extend_history_aliases(
    state: _HistoryObservationState,
    pending_aliases: Sequence[tuple[str, str]],
) -> None:
    for old_path, current in pending_aliases:
        existing = state.aliases.get(old_path)
        if existing is not None and existing != current:
            raise ValueError("Git history rename lineage is ambiguous")
        state.aliases[old_path] = current


def _record_history_cochanges(
    state: _HistoryObservationState,
    touched: set[str],
    config: GitHistoryConfig,
) -> None:
    if not touched:
        return
    state.counters["commits_touching_current_files"] += 1
    if len(touched) > config.max_files_per_cochange_commit:
        state.counters["cochange_commits_skipped_large"] += 1
        return
    ordered = sorted(touched)
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            state.cochanges[(left, right)] += 1


def _record_history_commit(
    state: _HistoryObservationState,
    commit: _GitCommit,
    config: GitHistoryConfig,
) -> None:
    touched: set[str] = set()
    pending_aliases: list[tuple[str, str]] = []
    for change in commit.changes:
        _record_history_change(state, commit, change, touched, pending_aliases)
    _extend_history_aliases(state, pending_aliases)
    _record_history_cochanges(state, touched, config)


def _observations(
    commits: Sequence[_GitCommit],
    owners: Mapping[str, ExternalEvidenceFile],
    config: GitHistoryConfig,
) -> tuple[
    dict[str, _HistoryAggregate],
    dict[tuple[str, str], int],
    dict[str, int],
]:
    state = _new_history_observation_state(owners)
    for commit in commits:
        _record_history_commit(state, commit, config)
    return state.aggregates, dict(state.cochanges), state.counters


@dataclass(frozen=True, slots=True)
class _GitHistoryWindow:
    config: GitHistoryConfig
    owners: dict[str, ExternalEvidenceFile]
    repository: GitRepositorySnapshot
    input_signature: str
    commits: tuple[_GitCommit, ...]
    history_truncated: bool
    start_timestamp: int
    end_timestamp: int
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int


def _collect_git_history_window(
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    environment: Mapping[str, str],
    *,
    config: GitHistoryConfig | None,
    snapshot: GitRepositorySnapshot | None,
    git_executable: str,
) -> _GitHistoryWindow:
    effective = GitHistoryConfig() if config is None else config
    resolved_root = _validated_root(root)
    owners = _owners_by_relative(files, effective)
    repository = (
        inspect_git_repository(
            resolved_root,
            environment,
            config=effective,
            git_executable=git_executable,
        )
        if snapshot is None
        else snapshot
    )
    signature = git_history_input_signature(files, repository, config=effective)
    log_run = _run_git(
        (
            "log",
            "--no-color",
            "--no-decorate",
            "--no-show-signature",
            "--no-ext-diff",
            "--no-textconv",
            "--topo-order",
            "--no-merges",
            "--find-renames=50%",
            f"--max-count={effective.max_commits + 1}",
            f"--format=%x00{_LOG_MARKER.decode('ascii')}%x00%H%x00%ct%x00%P%x00",
            "--numstat",
            "-z",
            "--end-of-options",
            repository.head_commit,
            "--",
        ),
        root=resolved_root,
        environment=environment,
        config=effective,
        git_executable=git_executable,
    )
    parsed = _parse_git_log(
        log_run.stdout,
        max_change_entries=effective.max_change_entries,
    )
    history_truncated = len(parsed) > effective.max_commits
    commits = parsed[: effective.max_commits]
    final_head, verify_stdout, verify_stderr = _resolve_root_and_head(
        resolved_root,
        environment,
        effective,
        git_executable,
    )
    if final_head != repository.head_commit:
        raise ValueError("Git history ref changed during observation")
    return _GitHistoryWindow(
        config=effective,
        owners=owners,
        repository=repository,
        input_signature=signature,
        commits=commits,
        history_truncated=history_truncated,
        start_timestamp=min(item.timestamp for item in commits),
        end_timestamp=max(item.timestamp for item in commits),
        stdout_bytes=repository.stdout_bytes + len(log_run.stdout) + verify_stdout,
        stderr_bytes=repository.stderr_bytes + len(log_run.stderr) + verify_stderr,
        process_invocations=repository.process_invocations + 2,
    )


def _git_history_shared_metadata(window: _GitHistoryWindow) -> dict[str, object]:
    return {
        "provider_schema": GIT_HISTORY_PROVIDER_SCHEMA,
        "history_input_signature": window.input_signature,
        "requested_ref": window.repository.requested_ref,
        "head_commit": window.repository.head_commit,
        "window_commits": len(window.commits),
        "window_start_timestamp": window.start_timestamp,
        "window_end_timestamp": window.end_timestamp,
        "history_truncated": window.history_truncated,
        "repository_shallow": window.repository.repository_shallow,
        "interpretation": "observed_history_not_defect_probability",
    }


def _git_history_metrics(
    window: _GitHistoryWindow,
    aggregates: Mapping[str, _HistoryAggregate],
    shared_metadata: Mapping[str, object],
) -> tuple[ExternalProviderMetric, ...]:
    metrics: list[ExternalProviderMetric] = []
    modules: dict[str, _HistoryAggregate] = {}
    module_files: defaultdict[str, int] = defaultdict(int)
    for path, owner in sorted(window.owners.items()):
        aggregate = aggregates[path]
        metrics.extend(
            _metrics_for_subject(
                subject_kind="file",
                subject_key=path,
                version_id=owner.version_id,
                aggregate=aggregate,
                window_commits=len(window.commits),
                reference_timestamp=window.end_timestamp,
                shared_metadata=shared_metadata,
            )
        )
        module = _module_from_path(path)
        if module is not None:
            modules.setdefault(module, _HistoryAggregate()).absorb(aggregate)
            module_files[module] += 1
    for module, aggregate in sorted(modules.items()):
        metrics.extend(
            _metrics_for_subject(
                subject_kind="module",
                subject_key=module,
                version_id=None,
                aggregate=aggregate,
                window_commits=len(window.commits),
                reference_timestamp=window.end_timestamp,
                shared_metadata={
                    **shared_metadata,
                    "current_module_files": module_files[module],
                },
            )
        )
    return tuple(metrics)


def _git_history_relations(
    window: _GitHistoryWindow,
    candidates: Mapping[tuple[str, str], int],
    shared_metadata: Mapping[str, object],
) -> tuple[tuple[ExternalProviderRelation, ...], int, bool]:
    ordered = sorted(candidates.items(), key=lambda item: (-item[1], item[0]))
    truncated = len(ordered) > window.config.max_relations
    selected = ordered[: window.config.max_relations]
    relations = tuple(
        ExternalProviderRelation(
            external_relation_identity(
                GIT_HISTORY_PROVIDER_ID,
                relation_kind="file_cochange",
                source_kind="file",
                source_key=left,
                target_kind="file",
                target_key=right,
                directed=False,
            ),
            "file_cochange",
            "file",
            left,
            "file",
            right,
            directed=False,
            confidence=None,
            source_version_id=window.owners[left].version_id,
            target_version_id=window.owners[right].version_id,
            metadata={
                **shared_metadata,
                "observed_commits_together": count,
                "observed_frequency_per_100_commits": (
                    count * 100.0 / len(window.commits)
                ),
                "relations_truncated": truncated,
                "interpretation": "cochange_observation_not_defect_probability",
            },
        )
        for (left, right), count in selected
    )
    return relations, len(ordered), truncated


def _git_history_limitations(
    window: _GitHistoryWindow,
    observation_counters: Mapping[str, int],
    *,
    files_with_history: int,
    relations_truncated: bool,
) -> tuple[str, ...]:
    limitations = ["merge_commits_excluded_from_churn_window"]
    if window.history_truncated:
        limitations.append("commit_window_truncated")
    if window.repository.repository_shallow:
        limitations.append("shallow_repository_history_incomplete")
    if relations_truncated:
        limitations.append("cochange_relation_candidates_truncated")
    if observation_counters["cochange_commits_skipped_large"]:
        limitations.append("large_commits_excluded_from_cochange_relations")
    if observation_counters["binary_or_unmeasured_entries"]:
        limitations.append("binary_numstat_line_counts_unavailable")
    if observation_counters["rename_entries"]:
        limitations.append("renames_followed_only_within_observed_window")
    if files_with_history < len(window.owners):
        limitations.append("some_current_files_absent_from_observed_history")
    return tuple(limitations)


def _git_history_counters(
    window: _GitHistoryWindow,
    observation_counters: Mapping[str, int],
    *,
    files_with_history: int,
    relation_candidates: int,
    relations_emitted: int,
    relations_truncated: bool,
    metrics_emitted: int,
    started_ns: int,
) -> dict[str, int]:
    return {
        "eligible_files": len(window.owners),
        # Every eligible file was evaluated against the complete bounded
        # history window. A file with no observed commit is still covered;
        # that absence is preserved separately instead of corrupting the
        # generic provider input-coverage contract.
        "covered_files": len(window.owners),
        "files_without_observed_history": len(window.owners) - files_with_history,
        "commits_requested": window.config.max_commits,
        "commits_observed": len(window.commits),
        "history_truncated": int(window.history_truncated),
        "repository_shallow": int(window.repository.repository_shallow),
        "relation_candidates": relation_candidates,
        "relations_emitted": relations_emitted,
        "relations_truncated": int(relations_truncated),
        "metrics_emitted": metrics_emitted,
        "process_invocations": window.process_invocations,
        "stdout_bytes": window.stdout_bytes,
        "stderr_bytes": window.stderr_bytes,
        "wall_milliseconds": max(0, (time.time_ns() - started_ns) // 1_000_000),
        **observation_counters,
    }


def _git_history_provenance(window: _GitHistoryWindow) -> dict[str, object]:
    return {
        "provider_id": GIT_HISTORY_PROVIDER_ID,
        "provider_schema": GIT_HISTORY_PROVIDER_SCHEMA,
        "source": "local_git_object_database",
        "requested_ref": window.repository.requested_ref,
        "head_commit": window.repository.head_commit,
        "repository_shallow": window.repository.repository_shallow,
        "history_input_signature": window.input_signature,
        "configuration_signature": window.config.signature,
        "configuration": window.config.as_payload(),
        "window": {
            "commits": len(window.commits),
            "start_timestamp": window.start_timestamp,
            "end_timestamp": window.end_timestamp,
            "truncated": window.history_truncated,
        },
        "authority": "advisory",
        "mutation_authority": False,
        "uses_network": False,
        "executes_content": False,
    }


def _git_history_execution(
    window: _GitHistoryWindow,
    metrics: Sequence[ExternalProviderMetric],
    relations: Sequence[ExternalProviderRelation],
    counters: Mapping[str, int],
    limitations: Sequence[str],
    *,
    relations_truncated: bool,
) -> GitHistoryExecution:
    return GitHistoryExecution(
        findings=(),
        metrics=tuple(sorted(metrics, key=lambda item: item.portable_metric_id)),
        relations=tuple(sorted(relations, key=lambda item: item.portable_relation_id)),
        history_input_signature=window.input_signature,
        configuration_signature=window.config.signature,
        requested_ref=window.repository.requested_ref,
        head_commit=window.repository.head_commit,
        repository_shallow=window.repository.repository_shallow,
        history_truncated=window.history_truncated,
        relations_truncated=relations_truncated,
        counters=counters,
        limitations=tuple(limitations),
        provenance=_git_history_provenance(window),
        stdout_bytes=window.stdout_bytes,
        stderr_bytes=window.stderr_bytes,
        process_invocations=window.process_invocations,
    )


def execute_git_history(
    root: Path,
    files: Sequence[ExternalEvidenceFile],
    environment: Mapping[str, str],
    *,
    config: GitHistoryConfig | None = None,
    snapshot: GitRepositorySnapshot | None = None,
    git_executable: str = "git",
) -> GitHistoryExecution:
    """Collect deterministic local history evidence under explicit hard bounds."""

    started_ns = time.time_ns()
    window = _collect_git_history_window(
        root,
        files,
        environment,
        config=config,
        snapshot=snapshot,
        git_executable=git_executable,
    )
    aggregates, relation_candidates, observation_counters = _observations(
        window.commits,
        window.owners,
        window.config,
    )
    shared_metadata = _git_history_shared_metadata(window)
    metrics = _git_history_metrics(window, aggregates, shared_metadata)
    relations, relation_candidate_count, relations_truncated = _git_history_relations(
        window,
        relation_candidates,
        shared_metadata,
    )
    files_with_history = sum(bool(item.commits) for item in aggregates.values())
    limitations = _git_history_limitations(
        window,
        observation_counters,
        files_with_history=files_with_history,
        relations_truncated=relations_truncated,
    )
    counters = _git_history_counters(
        window,
        observation_counters,
        files_with_history=files_with_history,
        relation_candidates=relation_candidate_count,
        relations_emitted=len(relations),
        relations_truncated=relations_truncated,
        metrics_emitted=len(metrics),
        started_ns=started_ns,
    )
    return _git_history_execution(
        window,
        metrics,
        relations,
        counters,
        limitations,
        relations_truncated=relations_truncated,
    )


__all__ = [
    "GIT_HISTORY_PROVIDER_ID",
    "GIT_HISTORY_PROVIDER_SCHEMA",
    "GitHistoryConfig",
    "GitHistoryExecution",
    "GitRepositorySnapshot",
    "execute_git_history",
    "git_history_input_signature",
    "inspect_git_repository",
]
