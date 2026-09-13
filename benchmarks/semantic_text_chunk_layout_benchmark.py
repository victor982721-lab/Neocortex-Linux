#!/usr/bin/env python3
"""Bounded, scratch-only comparison of Semantic ``text_chunks`` layouts.

The source is opened only through the repository's ``SQLiteReadSession`` in
``immutable_strict`` mode.  The selected ``text_chunks`` tuples and their
required ``semantic_items`` parents are copied byte-for-byte into a private
temporary fixture.  Three fresh scratch owners then compare the current
``WITHOUT ROWID`` layout, a bounded UPSERT/refresh replay, and the source DDL
with ``WITHOUT ROWID`` removed plus an explicit textual-PK ``NOT NULL``
invariant.  No corpus, model, vector owner, or
product database is opened for writing.

This is an instrument, not a migration.  It never calls VACUUM, ANALYZE, GC,
or a repository writer, and it refuses to overwrite its JSON output.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import math
import os
import platform
import re
import resource
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_SCHEMA = "neocortex.semantic-text-chunk-layout-benchmark/v1"
DEFAULT_BATCH_SIZE = 32
DEFAULT_READ_REPEATS = 3
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TOTAL_TIMEOUT_SECONDS = 1_800.0
MAX_BATCH_SIZE = 1_024
MAX_READ_REPEATS = 10
MAX_ROWS = 100_000
MAX_QUERY_ROWS = 20_000
PROGRESS_INTERVAL = 1_000
HASH_BLOCK_BYTES = 1024 * 1024
MAX_MEMORY_CGROUP_BYTES = 4_000_000_000
MAX_TEMPORARY_TOTAL_BYTES = 19_000_000_000
MAX_SINGLE_BLOB_BYTES = 128 * 1024 * 1024
MAX_CAPTURE_BATCH_BYTES = 256 * 1024 * 1024
MAX_CHECK_RESULTS = 1_000

REQUIRED_CHUNK_COLUMNS = frozenset(
    {
        "chunk_id",
        "item_id",
        "ordinal",
        "section_kind",
        "section_id",
        "start_char",
        "end_char",
        "text_zlib",
        "text_chars",
        "content_xxh3_128",
        "content_bytes",
        "content_xxh3_64_guard",
        "chunking_signature",
        "provenance_json",
        "refresh_token",
        "active",
        "updated_ns",
    }
)
REQUIRED_PARENT_COLUMNS = frozenset({"item_id"})
REFRESH_COLUMNS = ("refresh_token", "active", "updated_ns")
SCRATCH_PRODUCT_ROOT_NAMES = (
    ".config/Neocortex",
    ".local/share/Neocortex",
    ".local/state/Neocortex",
    ".cache/Neocortex",
)


class BenchmarkConfigurationError(ValueError):
    """The requested benchmark is outside its bounded safety contract."""


class BenchmarkExecutionError(RuntimeError):
    """The source, fixture, scratch case, or preservation gate failed."""


@dataclass(frozen=True, slots=True)
class Seed:
    """One in-memory query seed; values never leave the process as fields."""

    chunk_id: str
    item_id: str
    chunking_signature: str
    refresh_token: str


@dataclass(slots=True)
class Fixture:
    """Private captured tuples and source schema needed for all cases."""

    path: Path
    parent_ddl: str
    chunk_ddl: str
    rowid_chunk_ddl: str
    index_ddls: tuple[str, ...]
    chunk_columns: tuple[str, ...]
    parent_columns: tuple[str, ...]
    row_count: int
    parent_count: int
    chunk_hash: str
    parent_hash: str
    chunk_metrics: dict[str, int]
    parent_metrics: dict[str, int]
    active_row_count: int
    active_group_count: int
    seeds: tuple[Seed, ...]
    foreign_key_hash: str
    page_size: int
    source_pragmas: dict[str, object]


@dataclass(slots=True)
class PhaseCounter:
    """Trace and progress counters for one scratch connection."""

    statement_kinds: Counter[str]
    progress_callbacks: int = 0
    diagnostics: bool = True

    @classmethod
    def create(cls, *, diagnostics: bool = True) -> "PhaseCounter":
        return cls(statement_kinds=Counter(), diagnostics=diagnostics)

    def trace(self, statement: str) -> None:
        if not self.diagnostics:
            return
        normalized = " ".join(statement.strip().split())
        if not normalized:
            return
        verb = normalized.split(" ", 1)[0].upper()
        self.statement_kinds[verb] += 1

    def progress(self) -> int:
        if self.diagnostics:
            self.progress_callbacks += 1
        return _deadline_progress()

    def payload(self) -> dict[str, object]:
        if not self.diagnostics:
            return {
                "status": "disabled",
                "reason": "primary_measurement_untraced",
            }
        return {
            "status": "diagnostics_canary",
            "statement_kinds": dict(sorted(self.statement_kinds.items())),
            "progress_callbacks": self.progress_callbacks,
            "vm_steps_lower_bound": self.progress_callbacks * PROGRESS_INTERVAL,
            "progress_interval": PROGRESS_INTERVAL,
        }


@dataclass(slots=True)
class ScratchCase:
    name: str
    path: Path
    connection: sqlite3.Connection
    trace: PhaseCounter
    phases: dict[str, dict[str, object]]
    rowid_layout: bool


@dataclass(slots=True)
class RunByteBudget:
    """Incremental size guard over the benchmark's own SQLite files."""

    root: Path
    maximum_bytes: int = MAX_TEMPORARY_TOTAL_BYTES
    files: dict[Path, int] = field(default_factory=dict)
    observed_bytes: int = 0

    def register_sqlite(self, path: Path) -> None:
        resolved_root = self.root.expanduser().resolve()
        resolved_path = path.expanduser().resolve()
        if not _path_is_within(resolved_path, resolved_root):
            raise BenchmarkExecutionError("scratch database escaped the run root")
        for candidate in (
            path,
            Path(f"{path}-journal"),
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
        ):
            self.files.setdefault(candidate, 0)

    def checkpoint(self) -> int:
        _deadline_check()
        total = 0
        for path in self.files:
            try:
                value = path.lstat()
            except FileNotFoundError:
                size = 0
            except OSError as exc:
                raise BenchmarkExecutionError(
                    f"cannot inspect scratch file size: {path.name}"
                ) from exc
            else:
                if path.is_symlink() or not stat.S_ISREG(value.st_mode):
                    raise BenchmarkExecutionError(
                        f"scratch file is not a regular non-symlink: {path.name}"
                    )
                size = int(value.st_size)
            self.files[path] = size
            total += size
        self.observed_bytes = total
        if total > self.maximum_bytes:
            raise BenchmarkExecutionError(
                f"scratch temporary-byte budget exceeded: {total}>{self.maximum_bytes}"
            )
        return total


_RUN_DEADLINE: ContextVar[float | None] = ContextVar(
    "semantic_text_layout_deadline",
    default=None,
)


def _deadline_check() -> None:
    deadline = _RUN_DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise BenchmarkExecutionError("benchmark total deadline exceeded")


def _deadline_progress() -> int:
    deadline = _RUN_DEADLINE.get()
    return 1 if deadline is not None and time.monotonic() >= deadline else 0


def _connection_timeout() -> float:
    deadline = _RUN_DEADLINE.get()
    if deadline is None:
        return DEFAULT_TIMEOUT_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BenchmarkExecutionError("benchmark total deadline exceeded")
    return min(DEFAULT_TIMEOUT_SECONDS, remaining)


def _cgroup_limits() -> dict[str, object]:
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
        cgroup_root = Path("/sys/fs/cgroup").resolve(strict=True)
    except (FileNotFoundError, OSError, UnicodeError) as exc:
        raise BenchmarkConfigurationError("cgroup v2 limits are not observable") from exc
    relative: str | None = None
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            relative = parts[2]
            break
    if relative is None or not relative.startswith("/"):
        raise BenchmarkConfigurationError("unified cgroup path is not observable")
    cgroup_path = (cgroup_root / relative.lstrip("/")).resolve(strict=True)
    if not _path_is_within(cgroup_path, cgroup_root):
        raise BenchmarkConfigurationError("cgroup path escaped the cgroup mount")

    def read_limit(name: str) -> int:
        try:
            raw = (cgroup_path / name).read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError, UnicodeError) as exc:
            raise BenchmarkConfigurationError(f"{name} is not observable") from exc
        if not raw or raw == "max":
            raise BenchmarkConfigurationError(f"{name} is not a finite limit")
        try:
            value = int(raw)
        except ValueError as exc:
            raise BenchmarkConfigurationError(f"{name} is not an integer limit") from exc
        if value < 0:
            raise BenchmarkConfigurationError(f"{name} is negative")
        return value

    memory_max = read_limit("memory.max")
    swap_max = read_limit("memory.swap.max")
    if memory_max <= 0 or memory_max > MAX_MEMORY_CGROUP_BYTES:
        raise BenchmarkConfigurationError(
            f"memory.max must be in 1..{MAX_MEMORY_CGROUP_BYTES}"
        )
    if swap_max != 0:
        raise BenchmarkConfigurationError("memory.swap.max must be 0")
    return {
        "path": str(cgroup_path),
        "memory_max_bytes": memory_max,
        "memory_swap_max_bytes": swap_max,
        "memory_limit_ok": True,
        "swap_limit_ok": True,
    }


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _safe_path(
    path: Path,
    *,
    label: str,
    repository_root: Path,
    allow_repository: bool = False,
) -> Path:
    if not path.is_absolute():
        raise BenchmarkConfigurationError(f"{label} must be absolute")
    raw = path.expanduser()
    cursor = Path(raw.anchor)
    for part in raw.parts[1:]:
        cursor /= part
        try:
            if cursor.is_symlink():
                raise BenchmarkConfigurationError(f"{label} must not contain symlink components")
        except OSError as exc:
            raise BenchmarkConfigurationError(f"{label} cannot be inspected") from exc
    selected = raw.resolve()
    repository = repository_root.expanduser().resolve()
    if not allow_repository and (
        _path_is_within(selected, repository) or _path_is_within(repository, selected)
    ):
        raise BenchmarkConfigurationError(f"{label} must not overlap the checkout")
    home = Path.home().resolve()
    product_roots = tuple(home / value for value in SCRATCH_PRODUCT_ROOT_NAMES)
    if any(
        _path_is_within(selected, root) or _path_is_within(root, selected)
        for root in product_roots
    ):
        raise BenchmarkConfigurationError(f"{label} must not overlap installed NeoCortex state")
    return selected


def _existing_directory_env(name: str, *, repository_root: Path) -> Path:
    """Read a caller-owned anchor; enforce write exclusions on destinations.

    An artifact anchor may contain both a frozen source checkout and the
    private HOME of the run. Treating that ancestry as a write destination
    would reject the isolated runtime itself. No directory is created here.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raise BenchmarkConfigurationError(f"{name} must be an absolute existing directory")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or candidate == Path(candidate.anchor):
        raise BenchmarkConfigurationError(f"{name} must be a bounded absolute directory")
    cursor = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise BenchmarkConfigurationError(f"{name} must not contain symlink components")
    selected = candidate.resolve()
    try:
        identity = selected.lstat()
    except OSError as exc:
        raise BenchmarkConfigurationError(f"{name} cannot be inspected") from exc
    if selected.is_symlink() or not stat.S_ISDIR(identity.st_mode):
        raise BenchmarkConfigurationError(f"{name} must be a real directory")
    return selected


def _audit_roots(*, repository_root: Path) -> tuple[Path, Path, Path]:
    lab_root = _existing_directory_env(
        "NEOCORTEX_AUDIT_LAB_ROOT",
        repository_root=repository_root,
    )
    artifact_root = _existing_directory_env(
        "NEOCORTEX_AUDIT_ARTIFACT_ROOT",
        repository_root=repository_root,
    )
    source_root = _existing_directory_env(
        "NEOCORTEX_AUDIT_SOURCE_ROOT",
        repository_root=repository_root,
    )
    if lab_root == artifact_root or not _path_is_within(lab_root, artifact_root):
        raise BenchmarkConfigurationError(
            "NEOCORTEX_AUDIT_LAB_ROOT must be inside NEOCORTEX_AUDIT_ARTIFACT_ROOT"
        )
    # A preserved, explicitly selected snapshot may be outside this run's
    # artifact tree. It is read-only input, never a scratch/output destination.
    return lab_root, artifact_root, source_root


def _source_path(
    path: Path,
    *,
    repository_root: Path,
    source_root: Path | None = None,
) -> Path:
    if source_root is None:
        source_root = _existing_directory_env(
            "NEOCORTEX_AUDIT_SOURCE_ROOT",
            repository_root=repository_root,
        )
    selected = _safe_path(
        path,
        label="--source-db",
        repository_root=repository_root,
        allow_repository=False,
    )
    expected = (source_root / "semantic.sqlite3").resolve()
    if selected != expected or selected.name != "semantic.sqlite3":
        raise BenchmarkConfigurationError(
            "--source-db must equal NEOCORTEX_AUDIT_SOURCE_ROOT/semantic.sqlite3"
        )
    try:
        stat_result = selected.lstat()
    except FileNotFoundError as exc:
        raise BenchmarkConfigurationError("--source-db does not exist") from exc
    if selected.is_symlink() or not selected.is_file() or stat_result.st_size <= 0:
        raise BenchmarkConfigurationError("--source-db must be a non-empty regular file")
    # A corpus root supplied by the caller is an explicit no-read boundary.
    corpus_root = os.environ.get("NEOCORTEX_CORPUS_ROOT")
    if corpus_root:
        try:
            if _path_is_within(selected.resolve(), Path(corpus_root).expanduser().resolve()):
                raise BenchmarkConfigurationError("--source-db overlaps NEOCORTEX_CORPUS_ROOT")
        except OSError as exc:
            raise BenchmarkConfigurationError("NEOCORTEX_CORPUS_ROOT cannot be inspected") from exc
    return selected.resolve()


def _effective_temp_parent(
    requested: Path | None,
    *,
    repository_root: Path,
    lab_root: Path | None = None,
) -> tuple[Path, contextlib.AbstractContextManager[object]]:
    if lab_root is None:
        lab_root = _existing_directory_env(
            "NEOCORTEX_AUDIT_LAB_ROOT",
            repository_root=repository_root,
        )
    if requested is None:
        owner = tempfile.TemporaryDirectory(
            prefix="neocortex-semantic-layout-",
            dir=str(lab_root),
        )
        return Path(owner.name), owner
    selected = _safe_path(requested, label="--temp-root", repository_root=repository_root)
    if not _path_is_within(selected, lab_root):
        raise BenchmarkConfigurationError(
            "--temp-root must be inside NEOCORTEX_AUDIT_LAB_ROOT"
        )
    try:
        stat_result = selected.lstat()
    except FileNotFoundError:
        raise BenchmarkConfigurationError("--temp-root must already exist") from None
    if selected.is_symlink() or not stat.S_ISDIR(stat_result.st_mode):
        raise BenchmarkConfigurationError("--temp-root must be a real directory")
    if stat.S_IMODE(stat_result.st_mode) & 0o002:
        raise BenchmarkConfigurationError("--temp-root must not be world-writable")
    return selected, contextlib.nullcontext()


def _private_environment(root: Path) -> dict[str, object]:
    marker_before = os.environ.get("NEOCORTEX_AUDIT_LAB_ROOT")
    if marker_before is None or not marker_before.strip():
        raise BenchmarkConfigurationError("NEOCORTEX_AUDIT_LAB_ROOT is required")
    lab_root = Path(marker_before).expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    if resolved_root == lab_root or not _path_is_within(resolved_root, lab_root):
        raise BenchmarkConfigurationError("run root must be inside the audit lab")
    directories = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_DATA_HOME": root / "data",
        "XDG_STATE_HOME": root / "state",
        "XDG_RUNTIME_DIR": root / "runtime",
        "XDG_DOCUMENTS_DIR": root / "documents",
        "TMPDIR": root / "tmp",
        "TMP": root / "tmp",
        "TEMP": root / "tmp",
        "HF_HOME": root / "model-cache" / "huggingface",
        "TORCH_HOME": root / "model-cache" / "torch",
    }
    cache_directories = {
        "HF_HUB_CACHE": root / "model-cache" / "huggingface" / "hub",
        "TRANSFORMERS_CACHE": root / "model-cache" / "transformers",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in cache_directories.values():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = {name: str(directory) for name, directory in directories.items()}
    values.update(
        {
            "HF_HUB_CACHE": str(cache_directories["HF_HUB_CACHE"]),
            "TRANSFORMERS_CACHE": str(cache_directories["TRANSFORMERS_CACHE"]),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PIP_NO_INDEX": "1",
            "DO_NOT_TRACK": "1",
            "ORT_DISABLE_TELEMETRY": "1",
        }
    )
    # Do not remove NEOCORTEX_AUDIT_LAB_ROOT: it is a caller-owned marker and
    # preserving it is a gate, not permission to read any audit path.
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "NEOCORTEX_CORPUS_ROOT",
        "HUGGINGFACE_HUB_CACHE",
    ):
        os.environ.pop(name, None)
    os.environ.update(values)

    observed: dict[str, dict[str, object]] = {}
    for name, directory in {**directories, **cache_directories}.items():
        resolved = directory.resolve()
        try:
            identity = resolved.lstat()
        except OSError as exc:
            raise BenchmarkConfigurationError(
                f"private benchmark directory cannot be inspected: {name}"
            ) from exc
        if not _path_is_within(resolved, resolved_root) or not stat.S_ISDIR(identity.st_mode):
            raise BenchmarkConfigurationError(
                f"private benchmark directory escaped run root: {name}"
            )
        if stat.S_IMODE(identity.st_mode) & 0o077:
            raise BenchmarkConfigurationError(
                f"private benchmark directory is not owner-only: {name}"
            )
        if os.environ.get(name) != str(resolved):
            raise BenchmarkConfigurationError(
                f"effective private environment is not isolated: {name}"
            )
        observed[name] = {
            "path": str(resolved),
            "under_run_root": True,
            "owner_only": True,
            "mode": stat.S_IMODE(identity.st_mode),
        }
    marker_after = os.environ.get("NEOCORTEX_AUDIT_LAB_ROOT")
    if marker_after != marker_before:
        raise BenchmarkConfigurationError("NEOCORTEX_AUDIT_LAB_ROOT marker changed")
    return {
        "marker_present_before": True,
        "marker_present_after": marker_after is not None,
        "marker_preserved": marker_after == marker_before,
        "lab_root": str(lab_root),
        "run_root": str(resolved_root),
        "variables": observed,
        "all_paths_under_run_root": all(
            bool(value.get("under_run_root")) for value in observed.values()
        ),
    }


def _output_path(
    path: Path,
    *,
    repository_root: Path,
    temp_root: Path | None,
    artifact_root: Path | None = None,
) -> Path:
    if artifact_root is None:
        artifact_root = _existing_directory_env(
            "NEOCORTEX_AUDIT_ARTIFACT_ROOT",
            repository_root=repository_root,
        )
    selected = _safe_path(path, label="--output", repository_root=repository_root)
    if temp_root is not None and _path_is_within(selected, temp_root):
        raise BenchmarkConfigurationError("--output must be outside --temp-root")
    if not _path_is_within(selected.parent, artifact_root):
        raise BenchmarkConfigurationError(
            "--output parent must be inside NEOCORTEX_AUDIT_ARTIFACT_ROOT"
        )
    source_raw = os.environ.get("NEOCORTEX_AUDIT_SOURCE_ROOT")
    if source_raw and _path_is_within(selected, Path(source_raw).resolve()):
        raise BenchmarkConfigurationError("--output must not modify the selected source root")
    if selected.exists() or selected.is_symlink():
        raise BenchmarkConfigurationError("refusing to overwrite existing output")
    try:
        parent_identity = selected.parent.lstat()
    except OSError as exc:
        raise BenchmarkConfigurationError("--output parent cannot be inspected") from exc
    if selected.parent.is_symlink() or not stat.S_ISDIR(parent_identity.st_mode):
        raise BenchmarkConfigurationError("--output parent must already be a directory")
    return selected


def _canonical_json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_report(path: Path, value: Mapping[str, object]) -> None:
    encoded = _canonical_json(value)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise BenchmarkConfigurationError("temporary report path already exists")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise BenchmarkConfigurationError("refusing to overwrite existing output") from exc
        temporary.unlink()
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(HASH_BLOCK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _hash_value(digest: Any, value: object) -> None:
    if value is None:
        token = b"N"
    elif isinstance(value, bool):
        token = b"B1" if value else b"B0"
    elif isinstance(value, int):
        token = b"I" + str(value).encode("ascii")
    elif isinstance(value, float):
        token = b"F" + repr(value).encode("ascii")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        token = b"X" + bytes(value)
    elif isinstance(value, str):
        token = b"T" + value.encode("utf-8")
    else:
        raise BenchmarkExecutionError("unsupported SQLite value type in captured tuple")
    digest.update(len(token).to_bytes(8, "big"))
    digest.update(token)


def _tuple_digest_header(table: str, columns: Sequence[str]) -> Any:
    digest = hashlib.sha256()
    digest.update(b"table\0")
    digest.update(table.encode("utf-8"))
    for column in columns:
        digest.update(b"column\0")
        digest.update(column.encode("utf-8"))
    return digest


def _update_tuple_digest(digest: Any, row: Sequence[object]) -> None:
    digest.update(b"row\0")
    for value in row:
        _hash_value(digest, value)


def _quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise BenchmarkExecutionError("invalid SQLite identifier")
    return '"' + value.replace('"', '""') + '"'


def _normalize_sql(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def _safe_ddl(value: object, *, kind: str) -> str:
    if not isinstance(value, str) or not value.strip() or ";" in value:
        raise BenchmarkExecutionError(f"source {kind} DDL is unavailable or unsafe")
    normalized = _normalize_sql(value)
    if kind == "table" and not normalized.startswith("create table "):
        raise BenchmarkExecutionError("source table DDL is not CREATE TABLE")
    if kind == "index" and not normalized.startswith("create index "):
        raise BenchmarkExecutionError("source index DDL is not CREATE INDEX")
    if any(token in normalized for token in (" attach ", " pragma ", " create trigger ", " virtual table ")):
        raise BenchmarkExecutionError("source DDL contains an unsupported operation")
    return value


def _remove_without_rowid(ddl: str) -> str:
    matches = list(re.finditer(r"\s+without\s+rowid\b", ddl, flags=re.IGNORECASE))
    if len(matches) != 1:
        raise BenchmarkExecutionError("text_chunks DDL must contain exactly one WITHOUT ROWID clause")
    match = matches[0]
    candidate = ddl[: match.start()] + ddl[match.end() :]
    pk_matches = list(
        re.finditer(
            r"\bchunk_id\b(\s+text\b)(\s+not\s+null\b)?(\s+primary\s+key\b)",
            candidate,
            flags=re.IGNORECASE,
        )
    )
    if len(pk_matches) != 1:
        raise BenchmarkExecutionError(
            "rowid candidate could not add the explicit textual PK NOT NULL invariant"
        )
    pk_match = pk_matches[0]
    if pk_match.group(2) is None:
        candidate = (
            candidate[: pk_match.start(3)]
            + " NOT NULL"
            + candidate[pk_match.start(3) :]
        )
    if _normalize_sql(candidate) == _normalize_sql(ddl):
        raise BenchmarkExecutionError("rowid candidate did not change its layout/invariant")
    return candidate


def _fence_payload(fence: Any) -> dict[str, object]:
    def identity(value: Any) -> dict[str, int]:
        return {
            "device": int(value.device),
            "inode": int(value.inode),
            "mode": int(value.mode),
            "size": int(value.size),
            "mtime_ns": int(value.mtime_ns),
            "ctime_ns": int(value.ctime_ns),
        }

    return {
        "main": identity(fence.main),
        "sidecars": [
            {"suffix": str(suffix), "identity": identity(value)}
            for suffix, value in fence.sidecars
        ],
    }


def _read_pragma(connection: sqlite3.Connection, name: str) -> object:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    return None if row is None else row[0]


def _pragma_payload(connection: sqlite3.Connection, names: Sequence[str]) -> dict[str, object]:
    return {name: _read_pragma(connection, name) for name in names}


def _table_info(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, ...], dict[str, tuple[str, int, int]]]:
    rows = connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    if not rows:
        raise BenchmarkExecutionError(f"required table {table!r} is missing")
    columns: list[str] = []
    details: dict[str, tuple[str, int, int]] = {}
    for row in rows:
        name = str(row[1])
        columns.append(name)
        details[name] = (str(row[2] or ""), int(row[3]), int(row[5]))
    return tuple(columns), details


def _foreign_key_hash(connection: sqlite3.Connection, table: str) -> str:
    rows = connection.execute(f"PRAGMA foreign_key_list({_quote_identifier(table)})").fetchall()
    digest = _tuple_digest_header(f"foreign_key_list:{table}", tuple(str(i) for i in range(8)))
    for row in rows:
        _update_tuple_digest(digest, tuple(row))
    return digest.hexdigest()


def _table_ddl(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None:
        raise BenchmarkExecutionError(f"required table {table!r} DDL is missing")
    return _safe_ddl(row[0], kind="table")


def _index_ddls(connection: sqlite3.Connection, table: str) -> tuple[tuple[str, str], ...]:
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_schema "
        "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY name",
        (table,),
    ).fetchall()
    result: list[tuple[str, str]] = []
    for row in rows:
        name = str(row[0])
        sql = _safe_ddl(row[1], kind="index")
        result.append((name, sql))
    return tuple(result)


def _schema_preflight(connection: sqlite3.Connection) -> dict[str, object]:
    chunk_columns, chunk_details = _table_info(connection, "text_chunks")
    parent_columns, parent_details = _table_info(connection, "semantic_items")
    if not REQUIRED_CHUNK_COLUMNS.issubset(chunk_columns):
        missing = sorted(REQUIRED_CHUNK_COLUMNS.difference(chunk_columns))
        raise BenchmarkExecutionError(f"text_chunks lacks required columns: {','.join(missing)}")
    if not REQUIRED_PARENT_COLUMNS.issubset(parent_columns):
        raise BenchmarkExecutionError("semantic_items lacks item_id")
    primary = [name for name, detail in chunk_details.items() if detail[2] == 1]
    if primary != ["chunk_id"] or chunk_details["chunk_id"][0].upper() != "TEXT":
        raise BenchmarkExecutionError("text_chunks must have textual chunk_id primary key")
    if chunk_details["chunk_id"][1] != 1:
        raise BenchmarkExecutionError("textual chunk_id primary key must reject NULL")
    if parent_details["item_id"][2] != 1:
        raise BenchmarkExecutionError("semantic_items.item_id must be the parent primary key")
    indexes = _index_ddls(connection, "text_chunks")
    if len(indexes) != 2:
        raise BenchmarkExecutionError(
            f"expected exactly two explicit text_chunks indexes; observed {len(indexes)}"
        )
    text_ddl = _table_ddl(connection, "text_chunks")
    if not re.search(r"\bwithout\s+rowid\b", text_ddl, flags=re.IGNORECASE):
        raise BenchmarkExecutionError("current text_chunks DDL is not WITHOUT ROWID")
    return {
        "chunk_columns": chunk_columns,
        "parent_columns": parent_columns,
        "parent_ddl": _table_ddl(connection, "semantic_items"),
        "chunk_ddl": text_ddl,
        "rowid_chunk_ddl": _remove_without_rowid(text_ddl),
        "index_names": tuple(name for name, _sql in indexes),
        "index_ddls": tuple(sql for _name, sql in indexes),
        "foreign_key_hash": _foreign_key_hash(connection, "text_chunks"),
    }


def _source_count(connection: sqlite3.Connection, *, limit: int | None) -> tuple[int, int]:
    _deadline_check()
    row = connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()
    if row is None:
        raise BenchmarkExecutionError("text_chunks count is unavailable")
    total = int(row[0])
    selected = total if limit is None else min(total, limit)
    if total <= 0 or selected <= 0:
        raise BenchmarkExecutionError("text_chunks has no rows for the requested fixture")
    if selected > MAX_ROWS:
        raise BenchmarkConfigurationError(f"fixture exceeds the {MAX_ROWS}-row bound")
    return total, selected


def _limit_sql(base: str, *, limit: int | None) -> tuple[str, tuple[object, ...]]:
    if limit is None:
        return base, ()
    return f"{base} LIMIT ?", (limit,)


def _selected_parent_ids(
    connection: sqlite3.Connection,
    *,
    limit: int | None,
) -> tuple[str, ...]:
    _deadline_check()
    if limit is None:
        sql = (
            "SELECT item_id FROM (SELECT item_id FROM text_chunks ORDER BY chunk_id) "
            "GROUP BY item_id ORDER BY item_id"
        )
        params: tuple[object, ...] = ()
    else:
        sql = (
            "SELECT item_id FROM (SELECT item_id FROM text_chunks ORDER BY chunk_id LIMIT ?) "
            "GROUP BY item_id ORDER BY item_id"
        )
        params = (limit,)
    result: list[str] = []
    for row in connection.execute(sql, params):
        _deadline_check()
        result.append(str(row[0]))
    return tuple(result)


def _insert_sql(table: str, columns: Sequence[str], *, upsert: bool) -> str:
    quoted_table = _quote_identifier(table)
    quoted_columns = ",".join(_quote_identifier(column) for column in columns)
    placeholders = ",".join("?" for _column in columns)
    sql = f"INSERT INTO {quoted_table}({quoted_columns}) VALUES({placeholders})"
    if upsert:
        updates = ",".join(
            f"{_quote_identifier(column)}=excluded.{_quote_identifier(column)}"
            for column in columns
            if column != "chunk_id"
        )
        sql += f" ON CONFLICT({_quote_identifier('chunk_id')}) DO UPDATE SET {updates}"
    return sql


def _row_payload_bytes(row: Sequence[object]) -> int:
    total = 0
    for value in row:
        if isinstance(value, memoryview):
            total += value.nbytes
        elif isinstance(value, (bytes, bytearray)):
            total += len(value)
        elif isinstance(value, str):
            total += len(value) * 4
    return total


def _begin(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def _commit(connection: sqlite3.Connection) -> None:
    connection.commit()


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


def _iter_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    columns: Sequence[str],
    order_by: str,
    limit: int | None = None,
) -> Iterator[tuple[object, ...]]:
    selected = ",".join(_quote_identifier(column) for column in columns)
    sql, params = _limit_sql(
        f"SELECT {selected} FROM {_quote_identifier(table)} "
        f"ORDER BY {_quote_identifier(order_by)}",
        limit=limit,
    )
    for row in connection.execute(sql, params):
        _deadline_check()
        yield tuple(row)


def _hash_and_metrics(
    connection: sqlite3.Connection,
    *,
    table: str,
    columns: Sequence[str],
    order_by: str,
    limit: int | None = None,
) -> tuple[str, int, dict[str, int], tuple[Seed, ...]]:
    digest = _tuple_digest_header(table, columns)
    count = 0
    metrics = {
        "text_zlib_bytes": 0,
        "content_bytes_sum": 0,
        "text_chars_sum": 0,
        "max_text_zlib_bytes": 0,
    }
    seed_positions: set[int] = set()
    if limit is not None and limit > 0:
        seed_positions = {0, limit // 2, limit - 1}
    seeds: list[Seed] = []
    index = {column: position for position, column in enumerate(columns)}
    for row in _iter_rows(
        connection,
        table=table,
        columns=columns,
        order_by=order_by,
        limit=limit,
    ):
        _update_tuple_digest(digest, row)
        if table == "text_chunks":
            blob = row[index["text_zlib"]]
            if not isinstance(blob, (bytes, bytearray, memoryview)):
                raise BenchmarkExecutionError("text_zlib is not a BLOB")
            raw_blob_size = blob.nbytes if isinstance(blob, memoryview) else len(blob)
            if raw_blob_size > MAX_SINGLE_BLOB_BYTES:
                raise BenchmarkExecutionError("text_zlib exceeds the per-BLOB bound")
            blob_bytes = len(bytes(blob))
            metrics["text_zlib_bytes"] += blob_bytes
            metrics["max_text_zlib_bytes"] = max(metrics["max_text_zlib_bytes"], blob_bytes)
            metrics["content_bytes_sum"] += int(row[index["content_bytes"]])
            metrics["text_chars_sum"] += int(row[index["text_chars"]])
            if count in seed_positions:
                seeds.append(
                    Seed(
                        chunk_id=str(row[index["chunk_id"]]),
                        item_id=str(row[index["item_id"]]),
                        chunking_signature=str(row[index["chunking_signature"]]),
                        refresh_token=str(row[index["refresh_token"]]),
                    )
                )
        count += 1
    return digest.hexdigest(), count, metrics, tuple(seeds)


def _insert_batches(
    connection: sqlite3.Connection,
    rows: Iterable[tuple[object, ...]],
    *,
    sql: str,
    batch_size: int,
    transaction_mode: str,
    budget: RunByteBudget | None = None,
) -> int:
    _deadline_check()
    count = 0
    in_transaction = False
    if transaction_mode == "one_run":
        _begin(connection)
        in_transaction = True
    try:
        batch: list[tuple[object, ...]] = []
        batch_payload_bytes = 0
        for row in rows:
            _deadline_check()
            batch.append(row)
            batch_payload_bytes += _row_payload_bytes(row)
            if batch_payload_bytes > MAX_CAPTURE_BATCH_BYTES:
                raise BenchmarkExecutionError("SQLite insert batch exceeds byte bound")
            if len(batch) < batch_size:
                continue
            if transaction_mode == "per_batch":
                _begin(connection)
            connection.executemany(sql, batch)
            count += len(batch)
            if transaction_mode == "per_batch":
                _commit(connection)
            if budget is not None:
                budget.checkpoint()
            batch = []
            batch_payload_bytes = 0
        if batch:
            if transaction_mode == "per_batch":
                _begin(connection)
            connection.executemany(sql, batch)
            count += len(batch)
            if transaction_mode == "per_batch":
                _commit(connection)
            if budget is not None:
                budget.checkpoint()
        if transaction_mode == "one_run" and in_transaction:
            _commit(connection)
            in_transaction = False
        if budget is not None:
            budget.checkpoint()
    except BaseException:
        if in_transaction or connection.in_transaction:
            _rollback(connection)
        raise
    return count


def _rusage() -> dict[str, float]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "user_seconds": float(usage.ru_utime),
        "system_seconds": float(usage.ru_stime),
        # Linux reports ru_maxrss in KiB; the fallback keeps the metric
        # observable when /proc is unavailable.
        "max_rss_bytes": float(usage.ru_maxrss) * 1024,
    }


def _proc_status() -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        text = Path("/proc/self/status").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return result
    for line in text.splitlines():
        name, separator, raw = line.partition(":")
        if not separator or name not in {"VmRSS", "VmHWM", "VmSize", "Threads"}:
            continue
        token = raw.strip().split(" ", 1)[0]
        try:
            result[name] = int(token) * (1 if name == "Threads" else 1024)
        except ValueError:
            continue
    return result


def _proc_io() -> dict[str, int] | None:
    try:
        text = Path("/proc/self/io").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    result: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        try:
            result[key.strip()] = int(raw.strip())
        except ValueError:
            continue
    return result


class _RssSampler:
    """Small Linux RSS sampler scoped to one measured phase."""

    def __init__(self, interval_seconds: float = 0.05) -> None:
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak_bytes = 0

    def _sample(self) -> None:
        status = _proc_status()
        self.peak_bytes = max(self.peak_bytes, status.get("VmRSS", 0))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def start(self) -> None:
        self._sample()
        self._thread = threading.Thread(
            target=self._run,
            name="semantic-text-layout-rss",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._sample()


def _phase_metrics(function: Callable[[], object]) -> tuple[object, dict[str, object]]:
    before_usage = _rusage()
    before_status = _proc_status()
    before_io = _proc_io()
    sampler = _RssSampler()
    sampler.start()
    started = time.perf_counter_ns()
    try:
        _deadline_check()
        result = function()
    finally:
        sampler.stop()
    _deadline_check()
    elapsed_ns = time.perf_counter_ns() - started
    after_usage = _rusage()
    after_status = _proc_status()
    after_io = _proc_io()
    ru_maxrss = int(after_usage.get("max_rss_bytes", 0))
    # VmHWM/ru_maxrss are process high-water values, not phase-local peaks.
    # Keep them observable separately and use only the phase sampler plus the
    # final RSS for the phase metric.
    max_rss = max(sampler.peak_bytes, after_status.get("VmRSS", 0))
    process_high_water_rss = max(
        before_status.get("VmHWM", 0),
        after_status.get("VmHWM", 0),
        ru_maxrss,
    )
    io_delta = None
    if before_io is not None and after_io is not None:
        io_delta = {
            key: after_io.get(key, 0) - before_io.get(key, 0)
            for key in sorted(set(before_io) | set(after_io))
        }
    metrics: dict[str, object] = {
        "elapsed_ns": elapsed_ns,
        "wall_seconds": elapsed_ns / 1_000_000_000,
        "cpu_user_seconds": after_usage["user_seconds"] - before_usage["user_seconds"],
        "cpu_system_seconds": after_usage["system_seconds"] - before_usage["system_seconds"],
        "cpu_seconds": (
            after_usage["user_seconds"]
            - before_usage["user_seconds"]
            + after_usage["system_seconds"]
            - before_usage["system_seconds"]
        ),
        "rss_start_bytes": before_status.get("VmRSS"),
        "rss_end_bytes": after_status.get("VmRSS"),
        "rss_peak_bytes": max_rss,
        "rss_delta_peak_bytes": max(0, max_rss - before_status.get("VmRSS", 0)),
        "process_high_water_rss_bytes": process_high_water_rss,
        "process_io_delta": io_delta,
    }
    return result, metrics


def _configure_scratch(connection: sqlite3.Connection, *, page_size: int) -> dict[str, object]:
    if not isinstance(page_size, int) or not 512 <= page_size <= 65536:
        raise BenchmarkExecutionError("source page size is outside SQLite bounds")
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA page_size={page_size}")
    connection.execute("PRAGMA auto_vacuum=0")
    journal_mode = _read_pragma(connection, "journal_mode")
    if str(journal_mode).casefold() != "delete":
        raise BenchmarkExecutionError("scratch database did not select rollback-journal mode")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA query_only=OFF")
    values = _pragma_payload(
        connection,
        (
            "page_size",
            "auto_vacuum",
            "journal_mode",
            "synchronous",
            "foreign_keys",
            "trusted_schema",
            "query_only",
        ),
    )
    if int(values["page_size"]) != page_size or int(values["auto_vacuum"]) != 0:
        raise BenchmarkExecutionError("scratch page-size/auto-vacuum guard failed")
    if int(values["foreign_keys"]) != 1 or int(values["trusted_schema"]) != 0:
        raise BenchmarkExecutionError("scratch SQLite safety PRAGMAs failed")
    if int(values["query_only"]) != 0:
        raise BenchmarkExecutionError("scratch connection unexpectedly became query-only")
    return values


def _create_fixture_database(
    path: Path,
    *,
    parent_ddl: str,
    chunk_ddl: str,
    index_ddls: Sequence[str],
    page_size: int,
    budget: RunByteBudget | None = None,
) -> sqlite3.Connection:
    if budget is not None:
        budget.register_sqlite(path)
        budget.checkpoint()
    connection = sqlite3.connect(
        path,
        timeout=_connection_timeout(),
        isolation_level=None,
    )
    try:
        connection.set_progress_handler(_deadline_progress, PROGRESS_INTERVAL)
        _configure_scratch(connection, page_size=page_size)
        _begin(connection)
        connection.execute(parent_ddl)
        connection.execute(chunk_ddl)
        # The captured input is also the indexed feed for grouped replay.
        # Without these source indexes each group would scan the entire
        # captured table, contaminating the layout comparison with feed cost.
        for statement in index_ddls:
            connection.execute(statement)
        _commit(connection)
        if budget is not None:
            budget.checkpoint()
        return connection
    except BaseException:
        _rollback(connection)
        connection.close()
        raise


def _capture_fixture(
    source: sqlite3.Connection,
    *,
    run_root: Path,
    schema: Mapping[str, object],
    limit: int | None,
    batch_size: int,
    budget: RunByteBudget,
) -> Fixture:
    _total, selected = _source_count(source, limit=limit)
    parent_ids = _selected_parent_ids(source, limit=limit)
    path = run_root / "captured-tuples.sqlite3"
    spool = _create_fixture_database(
        path,
        parent_ddl=str(schema["parent_ddl"]),
        chunk_ddl=str(schema["chunk_ddl"]),
        index_ddls=tuple(str(value) for value in schema["index_ddls"]),
        page_size=int(schema["page_size"]),
        budget=budget,
    )
    try:
        parent_columns = tuple(str(value) for value in schema["parent_columns"])
        chunk_columns = tuple(str(value) for value in schema["chunk_columns"])
        parent_insert = _insert_sql("semantic_items", parent_columns, upsert=False)
        chunk_insert = _insert_sql("text_chunks", chunk_columns, upsert=False)
        parent_digest = _tuple_digest_header("semantic_items", parent_columns)
        parent_count = 0
        for start in range(0, len(parent_ids), min(batch_size, 400)):
            group = parent_ids[start : start + min(batch_size, 400)]
            placeholders = ",".join("?" for _value in group)
            sql = (
                f"SELECT {','.join(_quote_identifier(column) for column in parent_columns)} "
                f"FROM semantic_items WHERE item_id IN ({placeholders}) ORDER BY item_id"
            )
            rows = [tuple(row) for row in source.execute(sql, group)]
            if len(rows) != len(group):
                raise BenchmarkExecutionError("a selected chunk lacks its semantic_items parent")
            _insert_batches(
                spool,
                rows,
                sql=parent_insert,
                batch_size=batch_size,
                transaction_mode="per_batch",
                budget=budget,
            )
            for row in rows:
                _update_tuple_digest(parent_digest, row)
                parent_count += 1
        if parent_count != len(parent_ids):
            raise BenchmarkExecutionError("required semantic_items parent count is inconsistent")

        chunk_digest = _tuple_digest_header("text_chunks", chunk_columns)
        chunk_count = 0
        chunk_metrics = {
            "text_zlib_bytes": 0,
            "content_bytes_sum": 0,
            "text_chars_sum": 0,
            "max_text_zlib_bytes": 0,
        }
        active_row_count = 0
        active_groups: set[tuple[str, str]] = set()
        seeds: list[Seed] = []
        selected_sql, selected_params = _limit_sql(
            f"SELECT {','.join(_quote_identifier(column) for column in chunk_columns)} "
            "FROM text_chunks ORDER BY chunk_id",
            limit=limit,
        )
        chunk_cursor = source.execute(selected_sql, selected_params)
        index = {column: position for position, column in enumerate(chunk_columns)}
        batch: list[tuple[object, ...]] = []
        batch_payload_bytes = 0
        for source_row in chunk_cursor:
            _deadline_check()
            row = tuple(source_row)
            _update_tuple_digest(chunk_digest, row)
            blob = row[index["text_zlib"]]
            if not isinstance(blob, (bytes, bytearray, memoryview)):
                raise BenchmarkExecutionError("text_zlib is not a BLOB")
            blob_size = blob.nbytes if isinstance(blob, memoryview) else len(blob)
            if blob_size > MAX_SINGLE_BLOB_BYTES:
                raise BenchmarkExecutionError("text_zlib exceeds the per-BLOB bound")
            blob_bytes = len(bytes(blob))
            chunk_metrics["text_zlib_bytes"] += blob_bytes
            chunk_metrics["max_text_zlib_bytes"] = max(
                chunk_metrics["max_text_zlib_bytes"], blob_bytes
            )
            chunk_metrics["content_bytes_sum"] += int(row[index["content_bytes"]])
            chunk_metrics["text_chars_sum"] += int(row[index["text_chars"]])
            if int(row[index["active"]]) == 1:
                active_row_count += 1
                active_groups.add(
                    (
                        str(row[index["item_id"]]),
                        str(row[index["chunking_signature"]]),
                    )
                )
            if chunk_count in {0, selected // 2, selected - 1}:
                seeds.append(
                    Seed(
                        chunk_id=str(row[index["chunk_id"]]),
                        item_id=str(row[index["item_id"]]),
                        chunking_signature=str(row[index["chunking_signature"]]),
                        refresh_token=str(row[index["refresh_token"]]),
                    )
                )
            batch.append(row)
            batch_payload_bytes += _row_payload_bytes(row)
            if batch_payload_bytes > MAX_CAPTURE_BATCH_BYTES:
                raise BenchmarkExecutionError("capture batch exceeds byte bound")
            chunk_count += 1
            if len(batch) == batch_size:
                _insert_batches(
                    spool,
                    batch,
                    sql=chunk_insert,
                    batch_size=batch_size,
                    transaction_mode="per_batch",
                    budget=budget,
                )
                batch = []
                batch_payload_bytes = 0
        if batch:
            _insert_batches(
                spool,
                batch,
                sql=chunk_insert,
                batch_size=batch_size,
                transaction_mode="per_batch",
                budget=budget,
            )
        if chunk_count != selected:
            raise BenchmarkExecutionError("selected source row count changed during capture")
        source_chunk_hash = chunk_digest.hexdigest()
        source_parent_hash = parent_digest.hexdigest()
        observed_chunk_hash, observed_chunk_count, _observed_metrics, _ = _hash_and_metrics(
            spool,
            table="text_chunks",
            columns=chunk_columns,
            order_by="chunk_id",
            limit=None,
        )
        observed_parent_hash, observed_parent_count, observed_parent_metrics, _ = _hash_and_metrics(
            spool,
            table="semantic_items",
            columns=parent_columns,
            order_by="item_id",
            limit=None,
        )
        if (
            observed_chunk_hash != source_chunk_hash
            or observed_chunk_count != chunk_count
            or observed_parent_hash != source_parent_hash
            or observed_parent_count != parent_count
        ):
            raise BenchmarkExecutionError("captured fixture differs from source tuples")
        if tuple(name for name, _sql in _index_ddls(spool, "text_chunks")) != tuple(schema["index_names"]):
            raise BenchmarkExecutionError("captured feed indexes differ from the source")
        spool.execute("PRAGMA query_only=ON")
        if int(_read_pragma(spool, "query_only")) != 1:
            raise BenchmarkExecutionError("captured fixture could not be fenced query-only")
        budget.checkpoint()
        return Fixture(
            path=path,
            parent_ddl=str(schema["parent_ddl"]),
            chunk_ddl=str(schema["chunk_ddl"]),
            rowid_chunk_ddl=str(schema["rowid_chunk_ddl"]),
            index_ddls=tuple(str(value) for value in schema["index_ddls"]),
            chunk_columns=chunk_columns,
            parent_columns=parent_columns,
            row_count=chunk_count,
            parent_count=parent_count,
            chunk_hash=source_chunk_hash,
            parent_hash=source_parent_hash,
            chunk_metrics=chunk_metrics,
            parent_metrics=observed_parent_metrics,
            active_row_count=active_row_count,
            active_group_count=len(active_groups),
            seeds=tuple(dict.fromkeys(seeds)),
            foreign_key_hash=str(schema["foreign_key_hash"]),
            page_size=int(schema["page_size"]),
            source_pragmas=dict(schema["source_pragmas"]),
        )
    finally:
        spool.close()


def _open_fixture_read(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=_connection_timeout(), uri=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.set_progress_handler(_deadline_progress, PROGRESS_INTERVAL)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if int(_read_pragma(connection, "query_only")) != 1:
            raise BenchmarkExecutionError("fixture query-only guard failed")
        return connection
    except BaseException:
        connection.close()
        raise


def _open_case(
    path: Path,
    *,
    fixture: Fixture,
    name: str,
    rowid_layout: bool,
    batch_size: int,
    budget: RunByteBudget,
    sql_diagnostics: bool,
) -> ScratchCase:
    budget.register_sqlite(path)
    budget.checkpoint()
    connection = sqlite3.connect(
        path,
        timeout=_connection_timeout(),
        isolation_level=None,
    )
    try:
        _configure_scratch(connection, page_size=fixture.page_size)
        trace = PhaseCounter.create(diagnostics=sql_diagnostics)
        if sql_diagnostics:
            connection.set_trace_callback(trace.trace)
        connection.set_progress_handler(trace.progress, PROGRESS_INTERVAL)
        _begin(connection)
        connection.execute(fixture.parent_ddl)
        connection.execute(fixture.rowid_chunk_ddl if rowid_layout else fixture.chunk_ddl)
        for ddl in fixture.index_ddls:
            connection.execute(ddl)
        _commit(connection)
        budget.checkpoint()
        return ScratchCase(
            name=name,
            path=path,
            connection=connection,
            trace=trace,
            phases={},
            rowid_layout=rowid_layout,
        )
    except BaseException:
        _rollback(connection)
        connection.close()
        raise


def _case_table_hash(
    case: ScratchCase,
    *,
    fixture: Fixture,
) -> tuple[str, int, dict[str, int]]:
    digest, count, metrics, _ = _hash_and_metrics(
        case.connection,
        table="text_chunks",
        columns=fixture.chunk_columns,
        order_by="chunk_id",
        limit=None,
    )
    return digest, count, metrics


def _case_parent_hash(case: ScratchCase, *, fixture: Fixture) -> tuple[str, int, dict[str, int]]:
    return _hash_and_metrics(
        case.connection,
        table="semantic_items",
        columns=fixture.parent_columns,
        order_by="item_id",
        limit=None,
    )[:3]


def _replay_refresh(
    case: ScratchCase,
    fixture_conn: sqlite3.Connection,
    *,
    batch_size: int,
    transaction_mode: str,
    budget: RunByteBudget,
) -> dict[str, object]:
    _deadline_check()
    group_rows = fixture_conn.execute(
        "SELECT item_id,chunking_signature FROM text_chunks "
        "WHERE active=1 GROUP BY item_id,chunking_signature "
        "ORDER BY item_id,chunking_signature"
    )
    update_count = 0
    group_count = 0
    statements = 0
    one_run_open = False
    if transaction_mode == "one_run":
        _begin(case.connection)
        one_run_open = True
    try:
        for group in group_rows:
            _deadline_check()
            item_id = str(group[0])
            signature = str(group[1])
            ids = fixture_conn.execute(
                "SELECT chunk_id FROM text_chunks "
                "WHERE item_id=? AND chunking_signature=? AND active=1 ORDER BY chunk_id",
                (item_id, signature),
            )
            batch: list[str] = []
            for row in ids:
                _deadline_check()
                batch.append(str(row[0]))
                if len(batch) < batch_size:
                    continue
                if transaction_mode == "per_batch":
                    _begin(case.connection)
                placeholders = ",".join("?" for _value in batch)
                sql = (
                    "UPDATE text_chunks SET refresh_token=refresh_token,"
                    "active=active,updated_ns=updated_ns WHERE item_id=? AND active=1 "
                    "AND chunking_signature=? AND chunk_id IN ("
                    + placeholders
                    + ")"
                )
                cursor = case.connection.execute(sql, (item_id, signature, *batch))
                update_count += max(0, int(cursor.rowcount))
                statements += 1
                if transaction_mode == "per_batch":
                    _commit(case.connection)
                budget.checkpoint()
                batch = []
            if batch:
                if transaction_mode == "per_batch":
                    _begin(case.connection)
                placeholders = ",".join("?" for _value in batch)
                sql = (
                    "UPDATE text_chunks SET refresh_token=refresh_token,"
                    "active=active,updated_ns=updated_ns WHERE item_id=? AND active=1 "
                    "AND chunking_signature=? AND chunk_id IN ("
                    + placeholders
                    + ")"
                )
                cursor = case.connection.execute(sql, (item_id, signature, *batch))
                update_count += max(0, int(cursor.rowcount))
                statements += 1
                if transaction_mode == "per_batch":
                    _commit(case.connection)
                budget.checkpoint()
            group_count += 1
        if one_run_open:
            _commit(case.connection)
            one_run_open = False
        budget.checkpoint()
    except BaseException:
        if one_run_open or case.connection.in_transaction:
            _rollback(case.connection)
        raise
    return {
        "set_columns": list(REFRESH_COLUMNS),
        "where_columns": ["item_id", "active", "chunking_signature", "chunk_id"],
        "source_preserving_assignments": True,
        "stale_deactivation_rows": 0,
        "groups": group_count,
        "rows_touched": update_count,
        "bounded_update_statements": statements,
        "transaction_mode": transaction_mode,
    }


def _storage_snapshot(case: ScratchCase, *, fixture: Fixture) -> dict[str, object]:
    page_count = int(_read_pragma(case.connection, "page_count"))
    page_size = int(_read_pragma(case.connection, "page_size"))
    freelist_count = int(_read_pragma(case.connection, "freelist_count"))
    file_size = case.path.stat().st_size if case.path.exists() else 0
    sidecars: dict[str, int] = {}
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(f"{case.path}{suffix}")
        try:
            sidecars[suffix.lstrip("-")] = int(sidecar.stat().st_size)
        except FileNotFoundError:
            sidecars[suffix.lstrip("-")] = 0
    dbstat_rows: list[dict[str, object]] = []
    try:
        rows = case.connection.execute(
            "SELECT name,pagetype,COUNT(*),SUM(pgsize),SUM(payload),SUM(unused),"
            "SUM(mx_payload) FROM dbstat GROUP BY name,pagetype ORDER BY name,pagetype"
        ).fetchall()
    except sqlite3.DatabaseError:
        rows = []
        dbstat_available = False
    else:
        dbstat_available = True
        for row in rows:
            dbstat_rows.append(
                {
                    "object": str(row[0]),
                    "pagetype": str(row[1]),
                    "pages": int(row[2]),
                    "physical_bytes": int(row[3] or 0),
                    "payload_bytes": int(row[4] or 0),
                    "unused_bytes": int(row[5] or 0),
                    "max_payload": int(row[6] or 0),
                }
            )
    explicit_names = {
        str(row[0])
        for row in case.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' AND tbl_name='text_chunks'"
        )
    }
    target_names = {"text_chunks", *explicit_names}
    target_rows = [row for row in dbstat_rows if row["object"] in target_names]
    target_total = {
        "physical_bytes": sum(int(row["physical_bytes"]) for row in target_rows),
        "payload_bytes": sum(int(row["payload_bytes"]) for row in target_rows),
        "unused_bytes": sum(int(row["unused_bytes"]) for row in target_rows),
        "overflow_pages": sum(
            int(row["pages"]) for row in target_rows if row["pagetype"] == "overflow"
        ),
        "overflow_bytes": sum(
            int(row["physical_bytes"]) for row in target_rows if row["pagetype"] == "overflow"
        ),
        "used_bytes": sum(
            int(row["physical_bytes"]) - int(row["unused_bytes"]) for row in target_rows
        ),
    }
    return {
        "database_file_bytes": int(file_size),
        "page_count": page_count,
        "page_size": page_size,
        "page_count_bytes": page_count * page_size,
        "freelist_count": freelist_count,
        "freelist_bytes": freelist_count * page_size,
        "sidecar_bytes": sidecars,
        "dbstat_available": dbstat_available,
        "target_objects": sorted(target_names),
        "target_totals": target_total,
        "dbstat": dbstat_rows,
        "no_reclaimability_claim": True,
    }


def _layout_metadata(case: ScratchCase, *, fixture: Fixture) -> dict[str, object]:
    row = case.connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='text_chunks'"
    ).fetchone()
    if row is None:
        raise BenchmarkExecutionError("scratch text_chunks DDL disappeared")
    actual_ddl = str(row[0])
    expected = fixture.rowid_chunk_ddl if case.rowid_layout else fixture.chunk_ddl
    table_list = case.connection.execute(
        "PRAGMA table_list('text_chunks')"
    ).fetchone()
    without_rowid = None if table_list is None else bool(int(table_list[4]))
    index_rows = case.connection.execute(
        'PRAGMA index_list("text_chunks")'
    ).fetchall()
    explicit_names = [
        str(row[1]) for row in index_rows if len(row) >= 4 and str(row[3]) == "c"
    ]
    return {
        "ddl_matches_expected": _normalize_sql(actual_ddl) == _normalize_sql(expected),
        "ddl_sha256": _hash_text(actual_ddl),
        "expected_ddl_sha256": _hash_text(expected),
        "without_rowid": without_rowid,
        "rowid_candidate_only_change": (
            _normalize_sql(actual_ddl) == _normalize_sql(fixture.rowid_chunk_ddl)
            if case.rowid_layout
            else _normalize_sql(actual_ddl) == _normalize_sql(fixture.chunk_ddl)
        ),
        "explicit_index_names": explicit_names,
        "explicit_index_count": len(explicit_names),
        "explicit_index_count_matches_source": len(explicit_names) == len(fixture.index_ddls),
        "foreign_key_hash": _foreign_key_hash(case.connection, "text_chunks"),
        "foreign_key_matches_source": _foreign_key_hash(case.connection, "text_chunks")
        == fixture.foreign_key_hash,
    }


def _pk_semantics_probe(
    case: ScratchCase,
    *,
    fixture: Fixture,
) -> dict[str, object]:
    """Probe PK NULL/duplicate/upsert behavior and roll every probe back."""

    _deadline_check()
    columns = fixture.chunk_columns
    selected = ",".join(_quote_identifier(column) for column in columns)
    row = case.connection.execute(
        f"SELECT {selected} FROM {_quote_identifier('text_chunks')} LIMIT 1"
    ).fetchone()
    if row is None:
        raise BenchmarkExecutionError("PK probe has no captured chunk row")
    values = list(row)
    key_position = columns.index("chunk_id")
    duplicate_sql = _insert_sql("text_chunks", columns, upsert=False)
    upsert_sql = _insert_sql("text_chunks", columns, upsert=True)

    def rejected(values_to_try: Sequence[object]) -> bool:
        _deadline_check()
        case.connection.execute("SAVEPOINT semantic_text_layout_pk_probe")
        try:
            try:
                case.connection.execute(duplicate_sql, tuple(values_to_try))
            except sqlite3.IntegrityError:
                return True
            except sqlite3.Error as exc:
                raise BenchmarkExecutionError("PK probe failed with an unexpected SQLite error") from exc
            return False
        finally:
            case.connection.execute("ROLLBACK TO semantic_text_layout_pk_probe")
            case.connection.execute("RELEASE semantic_text_layout_pk_probe")

    null_values = list(values)
    null_values[key_position] = None
    null_rejected = rejected(null_values)
    duplicate_rejected = rejected(values)

    _deadline_check()
    before_count = int(
        case.connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0]
    )
    case.connection.execute("SAVEPOINT semantic_text_layout_pk_probe")
    try:
        try:
            case.connection.execute(upsert_sql, tuple(values))
            after_count = int(
                case.connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0]
            )
            conflict_preserved = after_count == before_count
        except sqlite3.Error as exc:
            raise BenchmarkExecutionError("PK upsert probe failed") from exc
    finally:
        case.connection.execute("ROLLBACK TO semantic_text_layout_pk_probe")
        case.connection.execute("RELEASE semantic_text_layout_pk_probe")
    return {
        "null_rejected": null_rejected,
        "duplicate_rejected": duplicate_rejected,
        "upsert_conflict_preserved": conflict_preserved,
        "rolled_back": not case.connection.in_transaction,
    }


def _integrity_checks(case: ScratchCase) -> dict[str, object]:
    _deadline_check()
    integrity = [
        str(row[0])
        for row in case.connection.execute(
            f"PRAGMA integrity_check({MAX_CHECK_RESULTS})"
        ).fetchmany(MAX_CHECK_RESULTS + 1)
    ]
    _deadline_check()
    quick = [
        str(row[0])
        for row in case.connection.execute(
            f"PRAGMA quick_check({MAX_CHECK_RESULTS})"
        ).fetchmany(MAX_CHECK_RESULTS + 1)
    ]
    _deadline_check()
    foreign = [
        tuple(row)
        for row in case.connection.execute("PRAGMA foreign_key_check").fetchmany(
            MAX_CHECK_RESULTS + 1
        )
    ]
    truncated = (
        len(integrity) > MAX_CHECK_RESULTS
        or len(quick) > MAX_CHECK_RESULTS
        or len(foreign) > MAX_CHECK_RESULTS
    )
    if len(integrity) > MAX_CHECK_RESULTS:
        integrity = integrity[:MAX_CHECK_RESULTS]
    if len(quick) > MAX_CHECK_RESULTS:
        quick = quick[:MAX_CHECK_RESULTS]
    if len(foreign) > MAX_CHECK_RESULTS:
        foreign = foreign[:MAX_CHECK_RESULTS]
    integrity_ok = integrity == ["ok"]
    quick_ok = quick == ["ok"]
    foreign_digest = _tuple_digest_header("foreign_key_check", ("row",)).hexdigest()
    if foreign:
        digest = _tuple_digest_header("foreign_key_check", tuple(str(i) for i in range(len(foreign[0]))))
        for row in foreign:
            _update_tuple_digest(digest, row)
        foreign_digest = digest.hexdigest()
    return {
        "integrity_check_ok": integrity_ok,
        "integrity_result_count": len(integrity),
        "quick_check_ok": quick_ok,
        "truncated": truncated,
        "quick_result_count": len(quick),
        "foreign_key_error_count": len(foreign),
        "foreign_key_error_hash": foreign_digest,
    }


def _plan_details(
    connection: sqlite3.Connection,
    sql: str,
    params: Sequence[object],
) -> dict[str, object]:
    _deadline_check()
    rows = connection.execute("EXPLAIN QUERY PLAN " + sql, tuple(params)).fetchall()
    if len(rows) > MAX_QUERY_ROWS:
        raise BenchmarkExecutionError("query plan exceeded its bounded row limit")
    details = [str(row[3]) for row in rows]
    return {
        "step_count": len(details),
        "details": details,
        "details_sha256": _hash_text("\n".join(details)),
        "scan_steps": sum("SCAN " in detail.upper() for detail in details),
        "temp_btree_steps": sum("TEMP B-TREE" in detail.upper() for detail in details),
        "index_steps": sum("INDEX" in detail.upper() for detail in details),
    }


def _query_specs() -> dict[str, tuple[str, Callable[[Seed], tuple[object, ...]]]]:
    return {
        "generation_lookup": (
            "SELECT chunk_id,item_id,content_xxh3_128,content_bytes," \
            "content_xxh3_64_guard,chunking_signature " \
            "FROM text_chunks WHERE chunk_id=? AND active=1",
            lambda seed: (seed.chunk_id,),
        ),
        "item_active": (
            "SELECT chunk_id,item_id,ordinal,section_kind,section_id,start_char,end_char,"
            "text_zlib,text_chars,content_xxh3_128,content_bytes,content_xxh3_64_guard,"
            "chunking_signature,provenance_json,refresh_token,active,updated_ns "
            "FROM text_chunks WHERE item_id=? AND active=1 AND chunking_signature=? "
            "ORDER BY ordinal",
            lambda seed: (seed.item_id, seed.chunking_signature),
        ),
        "refresh": (
            "SELECT chunk_id,refresh_token,active,updated_ns FROM text_chunks "
            "WHERE item_id=? AND chunking_signature=? AND refresh_token=? AND active=1 "
            "ORDER BY ordinal",
            lambda seed: (seed.item_id, seed.chunking_signature, seed.refresh_token),
        ),
    }


def _query_reads(
    case: ScratchCase,
    *,
    fixture: Fixture,
    repeats: int,
) -> dict[str, object]:
    _deadline_check()
    if not fixture.seeds:
        raise BenchmarkExecutionError("no query seeds were captured")
    plans: dict[str, object] = {}
    measurements: list[dict[str, object]] = []
    for name, (sql, params_for) in _query_specs().items():
        _deadline_check()
        plans[name] = _plan_details(case.connection, sql, params_for(fixture.seeds[0]))
        latencies: list[int] = []
        result_digest = _tuple_digest_header(f"query:{name}", ("rows",))
        result_rows = 0
        for seed in fixture.seeds:
            _deadline_check()
            params = params_for(seed)
            for _repeat in range(repeats):
                _deadline_check()
                started = time.perf_counter_ns()
                cursor = case.connection.execute(sql, params)
                rows = cursor.fetchmany(MAX_QUERY_ROWS + 1)
                if len(rows) > MAX_QUERY_ROWS:
                    raise BenchmarkExecutionError(
                        f"{name} query exceeded the {MAX_QUERY_ROWS}-row read bound"
                    )
                elapsed_ns = time.perf_counter_ns() - started
                latencies.append(elapsed_ns)
                result_rows += len(rows)
                for row in rows:
                    _deadline_check()
                    _update_tuple_digest(result_digest, tuple(row))
        ordered = sorted(latencies)
        measurements.append(
            {
                "query": name,
                "seed_count": len(fixture.seeds),
                "repeats": repeats,
                "observations": len(ordered),
                "rows_total": result_rows,
                "latency_ns_p50": ordered[(len(ordered) - 1) * 50 // 100],
                "latency_ns_p95": ordered[(len(ordered) - 1) * 95 // 100],
                "latency_ns_max": ordered[-1],
                "result_sha256": result_digest.hexdigest(),
            }
        )
    return {"plans": plans, "latencies": measurements}


def _run_case(
    *,
    run_root: Path,
    fixture: Fixture,
    name: str,
    rowid_layout: bool,
    batch_size: int,
    transaction_mode: str,
    read_repeats: int,
    budget: RunByteBudget,
    sql_diagnostics: bool,
) -> dict[str, object]:
    path = run_root / f"{name}.sqlite3"
    fixture_connection = _open_fixture_read(fixture.path)
    case: ScratchCase | None = None
    try:
        budget.checkpoint()
        case = _open_case(
            path,
            fixture=fixture,
            name=name,
            rowid_layout=rowid_layout,
            batch_size=batch_size,
            budget=budget,
            sql_diagnostics=sql_diagnostics,
        )
        insert_sql = _insert_sql(
            "text_chunks",
            fixture.chunk_columns,
            upsert=name != "T0",
        )
        parent_insert_sql = _insert_sql("semantic_items", fixture.parent_columns, upsert=False)
        parent_loaded, case.phases["parent_load"] = _phase_metrics(
            lambda: _insert_batches(
                case.connection,
                _iter_rows(
                    fixture_connection,
                    table="semantic_items",
                    columns=fixture.parent_columns,
                    order_by="item_id",
                ),
                sql=parent_insert_sql,
                batch_size=batch_size,
                transaction_mode=transaction_mode,
                budget=budget,
            )
        )
        case.phases["parent_load"]["rows"] = int(parent_loaded)
        chunk_loaded, case.phases["chunk_load"] = _phase_metrics(
            lambda: _insert_batches(
                case.connection,
                _iter_rows(
                    fixture_connection,
                    table="text_chunks",
                    columns=fixture.chunk_columns,
                    order_by="chunk_id",
                ),
                sql=insert_sql,
                batch_size=batch_size,
                transaction_mode=transaction_mode,
                budget=budget,
            )
        )
        case.phases["chunk_load"]["rows"] = int(chunk_loaded)
        initial_hash, initial_count, initial_metrics = _case_table_hash(case, fixture=fixture)
        if initial_hash != fixture.chunk_hash or initial_count != fixture.row_count:
            raise BenchmarkExecutionError("initial scratch fixture mismatch before replay")
        replay: dict[str, object] = {
            "upsert_columns": [column for column in fixture.chunk_columns if column != "chunk_id"],
            "conflict_passes": 0,
            "conflict_rows": 0,
            "refresh": None,
            "expected_refresh_rows": fixture.active_row_count,
            "expected_refresh_groups": fixture.active_group_count,
            "transaction_mode": transaction_mode,
        }
        if name != "T0":
            replay["conflict_passes"] = 1
            conflict_rows, case.phases["conflict_upsert_replay"] = _phase_metrics(
                lambda: _insert_batches(
                    case.connection,
                    _iter_rows(
                        fixture_connection,
                        table="text_chunks",
                        columns=fixture.chunk_columns,
                        order_by="chunk_id",
                    ),
                    sql=insert_sql,
                    batch_size=batch_size,
                    transaction_mode=transaction_mode,
                    budget=budget,
                )
            )
            case.phases["conflict_upsert_replay"]["rows"] = int(conflict_rows)
            replay["conflict_rows"] = int(conflict_rows)
            seed = fixture.seeds[0]
            replay["input_feed_plan"] = _plan_details(
                fixture_connection,
                "SELECT chunk_id FROM text_chunks "
                "WHERE item_id=? AND chunking_signature=? AND active=1 ORDER BY chunk_id",
                (seed.item_id, seed.chunking_signature),
            )
            refresh_result, case.phases["refresh_replay"] = _phase_metrics(
                lambda: _replay_refresh(
                    case,
                    fixture_connection,
                    batch_size=batch_size,
                    transaction_mode=transaction_mode,
                    budget=budget,
                )
            )
            replay["refresh"] = refresh_result
        final_hash, final_count, final_metrics = _case_table_hash(case, fixture=fixture)
        parent_hash, parent_count, _parent_metrics = _case_parent_hash(case, fixture=fixture)
        validation_result, case.phases["validation"] = _phase_metrics(
            lambda: {
                "integrity": _integrity_checks(case),
                "layout": _layout_metadata(case, fixture=fixture),
                "pk_semantics": _pk_semantics_probe(case, fixture=fixture),
            }
        )
        integrity = validation_result["integrity"]
        layout = validation_result["layout"]
        pk_semantics = validation_result["pk_semantics"]
        reads, case.phases["read_queries"] = _phase_metrics(
            lambda: _query_reads(case, fixture=fixture, repeats=read_repeats)
        )
        storage = _storage_snapshot(case, fixture=fixture)
        budget.checkpoint()
        return {
            "case": name,
            "rowid_layout": rowid_layout,
            "status": "complete",
            "logical_fixture": {
                "rows": fixture.row_count,
                "parent_rows": fixture.parent_count,
                "text_zlib_bytes": fixture.chunk_metrics["text_zlib_bytes"],
                "content_bytes_sum": fixture.chunk_metrics["content_bytes_sum"],
                "text_chars_sum": fixture.chunk_metrics["text_chars_sum"],
                "raw_tuple_hash": final_hash,
                "expected_raw_tuple_hash": fixture.chunk_hash,
                "raw_tuple_hash_equal": final_hash == fixture.chunk_hash,
                "row_count_equal": final_count == fixture.row_count,
                "initial_raw_tuple_hash_equal": initial_hash == fixture.chunk_hash,
                "initial_row_count_equal": initial_count == fixture.row_count,
                "parent_hash": parent_hash,
                "expected_parent_hash": fixture.parent_hash,
                "parent_hash_equal": parent_hash == fixture.parent_hash,
                "parent_count_equal": parent_count == fixture.parent_count,
                "final_metrics": final_metrics,
                "initial_metrics": initial_metrics,
                "initial_raw_tuple_hash": initial_hash,
                "initial_row_count": initial_count,
                "no_decompress_or_recompress": True,
            },
            "replay": replay,
            "integrity": integrity,
            "layout": layout,
            "pk_semantics": pk_semantics,
            "storage": storage,
            "query": reads,
            "phases": case.phases,
            "sql_trace": case.trace.payload(),
        }
    finally:
        fixture_connection.close()
        if case is not None:
            case.connection.set_trace_callback(None)
            case.connection.set_progress_handler(None, 0)
            case.connection.close()


def _check_case_result(result: Mapping[str, object]) -> list[str]:
    reasons: list[str] = []
    logical = result.get("logical_fixture")
    if not isinstance(logical, Mapping):
        reasons.append("logical_fixture_missing")
    else:
        for key in (
            "raw_tuple_hash_equal",
            "row_count_equal",
            "initial_raw_tuple_hash_equal",
            "initial_row_count_equal",
            "parent_hash_equal",
            "parent_count_equal",
            "no_decompress_or_recompress",
        ):
            if logical.get(key) is not True:
                reasons.append(key)
    integrity = result.get("integrity")
    if not isinstance(integrity, Mapping):
        reasons.append("integrity_missing")
    else:
        if integrity.get("integrity_check_ok") is not True:
            reasons.append("integrity_check")
        if integrity.get("quick_check_ok") is not True:
            reasons.append("quick_check")
        if integrity.get("truncated") is not False:
            reasons.append("integrity_truncated")
        if integrity.get("foreign_key_error_count") != 0:
            reasons.append("foreign_key_check")
    layout = result.get("layout")
    if not isinstance(layout, Mapping):
        reasons.append("layout_missing")
    else:
        if layout.get("ddl_matches_expected") is not True:
            reasons.append("ddl_mismatch")
        if layout.get("rowid_candidate_only_change") is not True:
            reasons.append("ddl_change_not_isolated")
        if layout.get("foreign_key_matches_source") is not True:
            reasons.append("foreign_key_ddl_mismatch")
        if layout.get("explicit_index_count_matches_source") is not True:
            reasons.append("explicit_index_count_mismatch")
        expected_without_rowid = not bool(result.get("rowid_layout"))
        if layout.get("without_rowid") is None:
            reasons.append("without_rowid_unavailable")
        elif layout.get("without_rowid") != expected_without_rowid:
            reasons.append("unexpected_rowid_layout")
    pk_semantics = result.get("pk_semantics")
    if not isinstance(pk_semantics, Mapping):
        reasons.append("pk_semantics_missing")
    else:
        for key in (
            "null_rejected",
            "duplicate_rejected",
            "upsert_conflict_preserved",
            "rolled_back",
        ):
            if pk_semantics.get(key) is not True:
                reasons.append(f"pk_{key}")
    replay = result.get("replay")
    case_name = result.get("case")
    if not isinstance(replay, Mapping):
        reasons.append("replay_missing")
    elif case_name == "T0":
        if replay.get("conflict_passes") != 0 or replay.get("refresh") is not None:
            reasons.append("t0_replay_unexpected")
    elif case_name in {"T1", "T2"}:
        if replay.get("conflict_passes") != 1:
            reasons.append("conflict_pass_count")
        logical_rows = logical.get("rows") if isinstance(logical, Mapping) else None
        if replay.get("conflict_rows") != logical_rows:
            reasons.append("conflict_row_count")
        refresh = replay.get("refresh")
        if not isinstance(refresh, Mapping):
            reasons.append("refresh_missing")
        else:
            if refresh.get("rows_touched") != replay.get("expected_refresh_rows"):
                reasons.append("refresh_row_count")
            if refresh.get("groups") != replay.get("expected_refresh_groups"):
                reasons.append("refresh_group_count")
            if refresh.get("stale_deactivation_rows") != 0:
                reasons.append("refresh_stale_deactivation")
            if refresh.get("source_preserving_assignments") is not True:
                reasons.append("refresh_not_source_preserving")
    storage = result.get("storage")
    if not isinstance(storage, Mapping) or storage.get("dbstat_available") is not True:
        reasons.append("dbstat_unavailable")
    return reasons


def _query_result_identity(result: Mapping[str, object]) -> dict[str, tuple[object, object]] | None:
    query = result.get("query")
    if not isinstance(query, Mapping):
        return None
    latencies = query.get("latencies")
    if not isinstance(latencies, list):
        return None
    identity: dict[str, tuple[object, object]] = {}
    for measurement in latencies:
        if not isinstance(measurement, Mapping):
            return None
        name = measurement.get("query")
        if not isinstance(name, str) or name in identity:
            return None
        identity[name] = (
            measurement.get("rows_total"),
            measurement.get("result_sha256"),
        )
    return identity


def _query_equivalence(cases: list[dict[str, object]]) -> dict[str, object]:
    complete_cases = [case for case in cases if case.get("status") == "complete"]
    if not complete_cases:
        return {
            "status": "not_assessed",
            "reason": "no_complete_case",
            "mismatches": [],
        }
    baseline = _query_result_identity(complete_cases[0])
    if baseline is None:
        mismatches = [str(complete_cases[0].get("case", "unknown"))]
    else:
        mismatches = []
        for case in complete_cases[1:]:
            observed = _query_result_identity(case)
            if observed != baseline:
                mismatches.append(str(case.get("case", "unknown")))
    if mismatches:
        for case in complete_cases:
            if str(case.get("case")) in mismatches or baseline is None:
                reasons = case.setdefault("failure_reasons", [])
                if isinstance(reasons, list) and "query_result_equivalence" not in reasons:
                    reasons.append("query_result_equivalence")
                case["status"] = "failed"
    return {
        "status": "complete" if not mismatches else "failed",
        "baseline_case": complete_cases[0].get("case"),
        "compared_fields": ["query", "rows_total", "result_sha256"],
        "plans_observation_only": True,
        "mismatches": mismatches,
    }


def _source_report_error(error: BaseException) -> dict[str, object]:
    return {
        "status": "failed",
        "error_type": type(error).__name__,
        "error_message_sha256": _hash_text(str(error)[:2_000]),
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True, help="absolute quiescent SQLite fixture/owner")
    parser.add_argument("--output", type=Path, required=True, help="new absolute JSON output path")
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--temp-root", type=Path, default=None, help="private absolute scratch parent")
    parser.add_argument("--limit", type=int, default=None, help="bounded canary rows; omit for all rows")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--transaction-mode",
        choices=("per_batch", "one_run"),
        default="per_batch",
        help="same transaction policy for T0/T1/T2",
    )
    parser.add_argument("--read-repeats", type=int, default=DEFAULT_READ_REPEATS)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"cooperative total deadline in seconds (0 < value <= {MAX_TOTAL_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--sql-diagnostics",
        action="store_true",
        help="enable bounded SQL verb diagnostics; only allowed with --limit <= 100",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and not 1 <= args.limit <= MAX_ROWS:
        parser.error(f"--limit must be between 1 and {MAX_ROWS}")
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        parser.error(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
    if not 1 <= args.read_repeats <= MAX_READ_REPEATS:
        parser.error(f"--read-repeats must be between 1 and {MAX_READ_REPEATS}")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be finite and positive")
    if args.timeout_seconds > MAX_TOTAL_TIMEOUT_SECONDS:
        parser.error(
            f"--timeout-seconds must not exceed {MAX_TOTAL_TIMEOUT_SECONDS:g} seconds"
        )
    if args.sql_diagnostics and (args.limit is None or args.limit > 100):
        parser.error("--sql-diagnostics requires --limit between 1 and 100")
    return args


def _temporary_budget_preflight(
    source: Path,
    anchor: Path,
    *,
    batch_size: int,
) -> dict[str, int | bool]:
    try:
        source_bytes = int(source.stat().st_size)
        anchor_stat = anchor.lstat()
        anchor_usage = os.statvfs(anchor)
        available_bytes = int(anchor_usage.f_bavail * anchor_usage.f_frsize)
    except OSError as exc:
        raise BenchmarkConfigurationError("temporary-byte preflight cannot inspect storage") from exc
    if not stat.S_ISDIR(anchor_stat.st_mode) or anchor.is_symlink():
        raise BenchmarkConfigurationError("temporary-byte preflight anchor is not a real directory")
    if source_bytes <= 0:
        raise BenchmarkConfigurationError("source database has no bytes for admission")
    copy_bytes = source_bytes * 4
    journal_reserve_bytes = source_bytes * 4
    batch_reserve_bytes = min(
        MAX_CAPTURE_BATCH_BYTES,
        max(1, batch_size) * 4 * 4_096,
    )
    estimated_bytes = source_bytes + copy_bytes + journal_reserve_bytes + batch_reserve_bytes
    if estimated_bytes > MAX_TEMPORARY_TOTAL_BYTES:
        raise BenchmarkConfigurationError(
            "conservative source plus four-copy scratch estimate exceeds the 19 GB limit"
        )
    if available_bytes < estimated_bytes:
        raise BenchmarkConfigurationError(
            "available bytes are below the conservative scratch admission estimate"
        )
    return {
        "source_bytes": source_bytes,
        "scratch_copy_count": 4,
        "scratch_copy_bytes": copy_bytes,
        "journal_reserve_bytes": journal_reserve_bytes,
        "batch_reserve_bytes": batch_reserve_bytes,
        "estimated_total_bytes": estimated_bytes,
        "available_bytes_before_run": available_bytes,
        "maximum_total_bytes": MAX_TEMPORARY_TOTAL_BYTES,
        "admission_ok": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    original_environment = dict(os.environ)
    original_sys_path = list(sys.path)
    original_tempdir = tempfile.tempdir
    repository_root = args.repository_root.expanduser().resolve()
    if not (repository_root / "neocortex" / "persistence" / "sqlite_immutable.py").is_file():
        raise BenchmarkConfigurationError("repository root lacks SQLiteReadSession")
    lab_root, artifact_root, source_root = _audit_roots(
        repository_root=repository_root,
    )
    cgroup = _cgroup_limits()
    source = _source_path(
        args.source_db,
        repository_root=repository_root,
        source_root=source_root,
    )
    output = _output_path(
        args.output,
        repository_root=repository_root,
        temp_root=None,
        artifact_root=artifact_root,
    )
    temp_anchor = lab_root
    if args.temp_root is not None:
        temp_anchor = _safe_path(
            args.temp_root,
            label="--temp-root",
            repository_root=repository_root,
        )
        if not _path_is_within(temp_anchor, lab_root):
            raise BenchmarkConfigurationError(
                "--temp-root must be inside NEOCORTEX_AUDIT_LAB_ROOT"
            )
    budget_preflight = _temporary_budget_preflight(
        source,
        temp_anchor,
        batch_size=args.batch_size,
    )
    deadline_token = _RUN_DEADLINE.set(time.monotonic() + args.timeout_seconds)
    temp_parent: Path | None = None
    parent_context: contextlib.AbstractContextManager[object] | None = None
    report: dict[str, object] = {
        "schema": BENCHMARK_SCHEMA,
        "finding": "SEM-A05",
        "status": "failed",
        "mode": "scratch_only",
        "source": {"owner": "semantic", "read_mode": "immutable_strict"},
        "limits": {
            "memory_max_bytes": int(cgroup["memory_max_bytes"]),
            "memory_swap_max_bytes": int(cgroup["memory_swap_max_bytes"]),
            "memory_limit_required_max_bytes": MAX_MEMORY_CGROUP_BYTES,
            "temporary_total_max_bytes": MAX_TEMPORARY_TOTAL_BYTES,
            "timeout_seconds": args.timeout_seconds,
            "sql_diagnostics": args.sql_diagnostics,
        },
        "execution": {
            "corpus_read": False,
            "models_loaded": False,
            "vectors_loaded": False,
            "product_state_written": False,
            "vacuum_analyze_gc": False,
        },
    }
    try:
        _deadline_check()
        temp_parent, parent_context = _effective_temp_parent(
            args.temp_root,
            repository_root=repository_root,
            lab_root=lab_root,
        )
        with parent_context:
            if _path_is_within(output, temp_parent):
                raise BenchmarkConfigurationError("--output must be outside --temp-root")
            with tempfile.TemporaryDirectory(
                prefix=f"run-{os.getpid()}-",
                dir=str(temp_parent),
            ) as run_name:
                run_root = Path(run_name)
                budget = RunByteBudget(run_root)
                environment = _private_environment(run_root)
                tempfile.tempdir = os.environ["TMPDIR"]
                report["environment"] = environment
                report["budget_preflight"] = budget_preflight
                report["guards"] = {
                    "lab_inside_artifact": _path_is_within(lab_root, artifact_root),
                    "source_root_explicit": source == source_root / "semantic.sqlite3",
                    "source_root_inside_artifact": _path_is_within(source_root, artifact_root),
                    "temp_parent_inside_lab": _path_is_within(temp_parent, lab_root),
                    "output_inside_artifact": _path_is_within(output.parent, artifact_root),
                    "output_outside_temp": not _path_is_within(output, temp_parent),
                    "cgroup_memory_limit_ok": bool(cgroup.get("memory_limit_ok")),
                    "cgroup_swap_limit_ok": bool(cgroup.get("swap_limit_ok")),
                }
                if str(repository_root) not in sys.path:
                    sys.path.insert(0, str(repository_root))
                from neocortex.persistence.sqlite_immutable import (
                    SQLiteReadMode,
                    SQLiteReadSession,
                    capture_sqlite_immutable_fence,
                )

                source_fence_before = capture_sqlite_immutable_fence(source)
                source_hash_before = _hash_file(source)
                source_fence_after_hash = capture_sqlite_immutable_fence(source)
                if source_fence_after_hash != source_fence_before:
                    raise BenchmarkExecutionError("source fence changed before immutable session")
                source_session = SQLiteReadSession(
                    source,
                    mode=SQLiteReadMode.IMMUTABLE_STRICT,
                    timeout_seconds=_connection_timeout(),
                    generation="semantic-text-chunk-layout-benchmark",
                )
                with source_session as source_connection:
                    if source_connection is None:
                        raise BenchmarkExecutionError("immutable source session returned no connection")
                    if source_session.source_fence != source_fence_before:
                        raise BenchmarkExecutionError("immutable session fence differs from preflight")
                    source_connection.set_progress_handler(_deadline_progress, PROGRESS_INTERVAL)
                    schema = _schema_preflight(source_connection)
                    source_pragmas = _pragma_payload(
                        source_connection,
                        (
                            "page_size",
                            "journal_mode",
                            "auto_vacuum",
                            "freelist_count",
                            "foreign_keys",
                            "query_only",
                            "trusted_schema",
                        ),
                    )
                    if int(source_pragmas["foreign_keys"]) != 1 or int(source_pragmas["query_only"]) != 1:
                        raise BenchmarkExecutionError("immutable source safeguards are not active")
                    schema["page_size"] = int(source_pragmas["page_size"])
                    schema["source_pragmas"] = source_pragmas
                    fixture = _capture_fixture(
                        source_connection,
                        run_root=run_root,
                        schema=schema,
                        limit=args.limit,
                        batch_size=args.batch_size,
                        budget=budget,
                    )
                    budget.checkpoint()
                source_fence_after = capture_sqlite_immutable_fence(source)
                source_hash_after = _hash_file(source)
                source_unchanged = (
                    source_fence_before == source_fence_after
                    and source_hash_before == source_hash_after
                )
                report["source"] = {
                    "owner": "semantic",
                    "read_mode": "immutable_strict",
                    "fence_before": _fence_payload(source_fence_before),
                    "fence_after": _fence_payload(source_fence_after),
                    "fence_unchanged": source_fence_before == source_fence_after,
                    "sha256_before": source_hash_before,
                    "sha256_after": source_hash_after,
                    "sha256_unchanged": source_hash_before == source_hash_after,
                    "unchanged": source_unchanged,
                }
                if not source_unchanged:
                    raise BenchmarkExecutionError("source fence or SHA-256 changed during benchmark")

                cases: list[dict[str, object]] = []
                for name, rowid_layout in (("T0", False), ("T1", False), ("T2", True)):
                    try:
                        result = _run_case(
                            run_root=run_root,
                            fixture=fixture,
                            name=name,
                            rowid_layout=rowid_layout,
                            batch_size=args.batch_size,
                            transaction_mode=args.transaction_mode,
                            read_repeats=args.read_repeats,
                            budget=budget,
                            sql_diagnostics=args.sql_diagnostics,
                        )
                        reasons = _check_case_result(result)
                        result["status"] = "complete" if not reasons else "failed"
                        result["failure_reasons"] = reasons
                    except Exception as error:
                        if _deadline_progress():
                            raise BenchmarkExecutionError(
                                "benchmark total deadline exceeded"
                            ) from error
                        result = {
                            "case": name,
                            "status": "failed",
                            "failure_reasons": [type(error).__name__],
                            "error_message_sha256": _hash_text(str(error)[:2_000]),
                        }
                    cases.append(result)
                query_equivalence = _query_equivalence(cases)
                budget.checkpoint()
                report.update(
                    {
                        "status": "complete" if source_unchanged and all(
                            case.get("status") == "complete" for case in cases
                        ) and query_equivalence.get("status") == "complete" else "failed",
                        "source": {
                            "owner": "semantic",
                            "read_mode": "immutable_strict",
                            "fence_before": _fence_payload(source_fence_before),
                            "fence_after": _fence_payload(source_fence_after),
                            "fence_unchanged": source_fence_before == source_fence_after,
                            "sha256_before": source_hash_before,
                            "sha256_after": source_hash_after,
                            "sha256_unchanged": source_hash_before == source_hash_after,
                            "unchanged": source_unchanged,
                            "pragmas": fixture.source_pragmas,
                            "active_rows": fixture.active_row_count,
                            "active_groups": fixture.active_group_count,
                        },
                        "fixture": {
                            "limit": args.limit,
                            "order": "chunk_id ASC",
                            "rows": fixture.row_count,
                            "required_parent_rows": fixture.parent_count,
                            "chunk_tuple_sha256": fixture.chunk_hash,
                            "parent_tuple_sha256": fixture.parent_hash,
                            "text_zlib_bytes": fixture.chunk_metrics["text_zlib_bytes"],
                            "content_bytes_sum": fixture.chunk_metrics["content_bytes_sum"],
                            "text_chars_sum": fixture.chunk_metrics["text_chars_sum"],
                            "max_text_zlib_bytes": fixture.chunk_metrics["max_text_zlib_bytes"],
                            "no_decompress_or_recompress": True,
                        },
                        "layout_contract": {
                            "source_text_ddl_sha256": _hash_text(fixture.chunk_ddl),
                            "rowid_candidate_ddl_sha256": _hash_text(fixture.rowid_chunk_ddl),
                            "rowid_candidate_change": (
                                "remove WITHOUT ROWID and add explicit chunk_id NOT NULL"
                            ),
                            "explicit_index_count": len(fixture.index_ddls),
                            "explicit_indexes_preserved": True,
                            "foreign_key_hash": fixture.foreign_key_hash,
                        },
                        "replay_contract": {
                            "upsert_columns": [
                                column for column in fixture.chunk_columns if column != "chunk_id"
                            ],
                            "refresh_set_columns": list(REFRESH_COLUMNS),
                            "refresh_where_columns": [
                                "item_id",
                                "active",
                                "chunking_signature",
                                "chunk_id",
                            ],
                            "stale_deactivation_modeled": False,
                            "transaction_mode": args.transaction_mode,
                            "batch_size": args.batch_size,
                        },
                        "cases": cases,
                        "query_equivalence": query_equivalence,
                        "scratch_budget_observed_bytes": budget.observed_bytes,
                        "controls": {
                            "R0": "not_run; observed revision control remains in audit A",
                            "no_savings_claim": True,
                            "no_schema_10_decision": True,
                        },
                        "runtime": {
                            "python": platform.python_version(),
                            "implementation": platform.python_implementation(),
                            "platform": platform.platform(aliased=True),
                            "private_environment": True,
                            "audit_lab_marker_preserved": "NEOCORTEX_AUDIT_LAB_ROOT" in original_environment
                            and os.environ.get("NEOCORTEX_AUDIT_LAB_ROOT")
                            == original_environment.get("NEOCORTEX_AUDIT_LAB_ROOT"),
                        },
                    }
                )
    except Exception as error:
        report.update(_source_report_error(error))
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
        sys.path[:] = original_sys_path
        tempfile.tempdir = original_tempdir
        _RUN_DEADLINE.reset(deadline_token)
    _write_report(output, report)
    return 0 if report.get("status") == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "BENCHMARK_SCHEMA",
    "BenchmarkConfigurationError",
    "BenchmarkExecutionError",
    "main",
)
