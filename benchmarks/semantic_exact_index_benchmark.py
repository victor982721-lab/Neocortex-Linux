#!/usr/bin/env python3
"""LAB benchmark for the opt-in persisted exact Semantic index.

This is a product-facing benchmark, not a product default.  It builds only the
repo-native synthetic fixture, prepares and reopens an explicit exact-index
artifact, and compares the legacy ``exact_index=None`` caller with the indexed
caller using the same public Page and bounded ResolvedSearchHit operations.
"""

from __future__ import annotations

import argparse
import contextlib
from functools import partial
import hashlib
import importlib
import importlib.util
import json
import math
import os
import platform
import resource
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

REPORT_SCHEMA = "neocortex.semantic.exact-index-product-benchmark/v1"
EXPECTED_SCHEMA = 10
SIZES = (100, 100_000, 500_000)
DEFAULT_REPEATS = 3
MAX_REPEATS = 5
MAX_SECONDS = 900.0
MAX_RSS = 4_000_000_000
MAX_TEMP = 19_000_000_000
INDEX_MAX_BYTES = 4_000_000_000
LIMIT = 20
BATCH = 512
SQL_INTERVAL = 1_000


class BenchError(RuntimeError):
    def __init__(self, message: str, *, phase: str, code: str = "benchmark_error") -> None:
        self.phase, self.code = phase, code
        super().__init__(message)


class DeadlineExceeded(BenchError):
    def __init__(self, phase: str) -> None:
        super().__init__(
            "monotonic benchmark deadline exceeded", phase=phase, code="deadline_exceeded"
        )


def _deadline_callback(deadline: int, phase: str) -> None:
    if time.monotonic_ns() > deadline:
        raise DeadlineExceeded(phase)


def _canon(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _real_dir(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise BenchError(
            f"{label} must be absolute", phase="preflight", code="absolute_path_required"
        )
    value = Path(os.path.abspath(path))
    current = value
    while True:
        if current.is_symlink():
            raise BenchError(f"{label} contains a symlink", phase="preflight", code="symlink_path")
        if current.parent == current:
            break
        current = current.parent
    if not value.is_dir() or value.is_symlink():
        raise BenchError(
            f"{label} is not a real directory", phase="preflight", code="directory_required"
        )
    return value


def _new_output(path: Path, repository_root: Path) -> Path:
    if not path.is_absolute():
        raise BenchError(
            "--output must be absolute", phase="preflight", code="absolute_path_required"
        )
    value = Path(os.path.abspath(path))
    current = value
    while True:
        if current.is_symlink():
            raise BenchError("output contains a symlink", phase="preflight", code="symlink_path")
        if current.parent == current:
            break
        current = current.parent
    if _inside(value, repository_root) or value.exists() or value.is_symlink():
        raise BenchError(
            "output must be new and outside the frozen checkout",
            phase="preflight",
            code="output_invalid",
        )
    if (
        value.parent.name != "derived"
        or value.parent.parent.name != "data"
        or not value.parent.is_dir()
    ):
        raise BenchError(
            "output must be under an existing data/derived directory",
            phase="preflight",
            code="output_directory_invalid",
        )
    return value


def _proc_status() -> dict[str, int]:
    try:
        text = Path(f"/proc/{os.getpid()}/status").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return {}
    result: dict[str, int] = {}
    for key in ("VmRSS", "VmSize", "Threads"):
        for line in text.splitlines():
            if not line.startswith(f"{key}:"):
                continue
            try:
                value = int(line.split(":", 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                continue
            result[key] = value if key == "Threads" else value * 1024
            break
    return result


def _proc_io() -> dict[str, int] | None:
    try:
        text = Path(f"/proc/{os.getpid()}/io").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    result: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        try:
            result[key.strip()] = int(raw.strip())
        except ValueError:
            pass
    return result


def _usage() -> dict[str, float]:
    value = resource.getrusage(resource.RUSAGE_SELF)
    return {"user_seconds": float(value.ru_utime), "system_seconds": float(value.ru_stime)}


def _delta(before: Mapping[str, float], after: Mapping[str, float]) -> dict[str, float]:
    return {key: float(after.get(key, 0.0) - before.get(key, 0.0)) for key in after}


def _io_delta(
    before: Mapping[str, int] | None, after: Mapping[str, int] | None
) -> dict[str, int] | None:
    if before is None or after is None:
        return None
    return {key: int(after.get(key, 0) - before.get(key, 0)) for key in after}


class _Sampler:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.peak_rss = self.peak_vms = self.peak_threads = 0

    def sample(self) -> None:
        value = _proc_status()
        self.peak_rss = max(self.peak_rss, value.get("VmRSS", 0))
        self.peak_vms = max(self.peak_vms, value.get("VmSize", 0))
        self.peak_threads = max(self.peak_threads, value.get("Threads", 0))

    def _run(self) -> None:
        while not self.stop_event.wait(0.05):
            self.sample()

    def start(self) -> None:
        self.sample()
        self.thread = threading.Thread(target=self._run, name="exact-index-rss", daemon=True)
        self.thread.start()

    def request_stop(self) -> None:
        self.stop_event.set()

    def join(self) -> None:
        if self.thread is not None:
            self.thread.join(timeout=1.0)


def _phase(
    name: str, fn: Callable[[], Any], *, deadline: int, phases: list[dict[str, object]]
) -> Any:
    before_usage, before_io, before_status = _usage(), _proc_io(), _proc_status()
    sampler = _Sampler()
    sampler.start()
    started = time.monotonic_ns()
    error: BaseException | None = None
    value: Any = None
    try:
        if time.monotonic_ns() > deadline:
            raise DeadlineExceeded(name)
        value = fn()
        if time.monotonic_ns() > deadline:
            raise DeadlineExceeded(name)
        return value
    except BaseException as exc:
        error = exc
        raise
    finally:
        sampler.request_stop()
        ended = time.monotonic_ns()
        after_usage, after_io, after_status = _usage(), _proc_io(), _proc_status()
        sampler.join()
        entry: dict[str, object] = {
            "name": name,
            "status": "error" if error else "complete",
            "wall_seconds": (ended - started) / 1_000_000_000,
            "cpu": _delta(before_usage, after_usage),
            "io_delta": _io_delta(before_io, after_io),
            "rss_start_bytes": before_status.get("VmRSS"),
            "rss_end_bytes": after_status.get("VmRSS"),
            "rss_peak_bytes": sampler.peak_rss,
            "vms_peak_bytes": sampler.peak_vms,
            "peak_threads": sampler.peak_threads,
        }
        if error:
            entry["error"] = {
                "type": type(error).__name__,
                "code": str(getattr(error, "code", "untyped_error")),
                "phase": str(getattr(error, "phase", name)),
                "message": str(error)[:1000],
            }
        phases.append(entry)
        if sampler.peak_rss > MAX_RSS:
            raise BenchError("RSS bound exceeded", phase=name, code="rss_bound_exceeded")


def _error(exc: BaseException, phase: str = "unknown") -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "code": str(getattr(exc, "code", "untyped_error")),
        "phase": str(getattr(exc, "phase", phase)),
        "message": str(exc)[:1000],
    }


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise BenchError(f"cannot load {path}", phase="fixture", code="source_loader_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _clear_neocortex() -> None:
    for name in tuple(sys.modules):
        if name == "neocortex" or name.startswith("neocortex."):
            del sys.modules[name]


def _private_environment(root: Path) -> dict[str, str]:
    paths = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_CONFIG_DIRS": root / "config",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_DATA_HOME": root / "data",
        "XDG_DATA_DIRS": root / "data",
        "XDG_STATE_HOME": root / "state",
        "XDG_RUNTIME_DIR": root / "runtime",
        "TMPDIR": root / "tmp",
        "TMP": root / "tmp",
        "TEMP": root / "tmp",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = {name: str(path) for name, path in paths.items()}
    values.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PIP_NO_INDEX": "1",
            "DO_NOT_TRACK": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "NEOCORTEX_CORPUS_ROOT",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
    ):
        os.environ.pop(name, None)
    os.environ.update(values)
    return values


def _dir_bytes(root: Path, deadline: int) -> int:
    total = 0
    for directory, _dirs, files in os.walk(root, followlinks=False):
        if time.monotonic_ns() > deadline:
            raise DeadlineExceeded("temporary_bound")
        for name in files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise BenchError(
                    "temporary symlink", phase="temporary_bound", code="temporary_symlink"
                )
            total += int(info.st_size)
            if total > MAX_TEMP:
                raise BenchError(
                    "temporary byte bound exceeded",
                    phase="temporary_bound",
                    code="temporary_bytes_bound_exceeded",
                )
    return total


def _guarded_db(original: Callable[..., Any], deadline: int) -> Callable[..., Any]:
    @contextlib.contextmanager
    def wrapper(path: Path, *args: object, **kwargs: object) -> Iterator[Any]:
        tripped = False

        def progress() -> int:
            nonlocal tripped
            if time.monotonic_ns() > deadline:
                tripped = True
                return 1
            return 0

        with original(path, *args, **kwargs) as connection:
            connection.set_progress_handler(progress, SQL_INTERVAL)
            try:
                yield connection
            except sqlite3.OperationalError as exc:
                if tripped:
                    raise DeadlineExceeded("sql_progress") from exc
                raise
            finally:
                try:
                    connection.set_progress_handler(None, 0)
                except sqlite3.ProgrammingError:
                    pass
            if tripped:
                raise DeadlineExceeded("sql_progress")

    return wrapper


def _install_db_guards(deadline: int) -> Callable[[], None]:
    modules = [
        importlib.import_module(name)
        for name in (
            "neocortex.semantic.semantic_schema",
            "neocortex.semantic.semantic_state",
            "neocortex.semantic.semantic_search_repository",
            "neocortex.semantic.semantic_exact_index",
        )
    ]
    originals: list[tuple[Any, Any]] = []
    for module in modules:
        original = getattr(module, "semantic_database", None)
        if original is not None:
            originals.append((module, original))
            module.semantic_database = _guarded_db(original, deadline)

    def restore() -> None:
        for module, original in reversed(originals):
            module.semantic_database = original

    return restore


def _patch_build(benchmark: Any, deadline: int) -> Callable[[], None]:
    file_bytes, proc_status = benchmark._file_bytes, benchmark._read_proc_status

    def checked_bytes(path: Path) -> dict[str, int]:
        if time.monotonic_ns() > deadline:
            raise DeadlineExceeded("build")
        result = file_bytes(path)
        if result.get("total", 0) > MAX_TEMP:
            raise BenchError(
                "fixture temporary bytes exceeded",
                phase="build",
                code="temporary_bytes_bound_exceeded",
            )
        return result

    def checked_status(pid: int = 0) -> dict[str, int] | None:
        if time.monotonic_ns() > deadline:
            raise DeadlineExceeded("build")
        result = proc_status(pid)
        if result and result.get("VmRSS", 0) > MAX_RSS:
            raise BenchError("fixture RSS exceeded", phase="build", code="rss_bound_exceeded")
        return result

    benchmark._file_bytes, benchmark._read_proc_status = checked_bytes, checked_status

    def restore() -> None:
        benchmark._file_bytes, benchmark._read_proc_status = file_bytes, proc_status

    return restore


def _model_payload(model: Any) -> dict[str, object]:
    return {
        "model_signature": str(model.model_signature),
        "vector_space": str(model.vector_space),
        "dimensions": int(model.dimensions),
        "modality": str(model.modality.value),
        "vector_dtype": str(model.vector_dtype.value),
        "model_id": str(model.model_id),
        "model_version": str(model.model_version),
        "provider": str(model.provider),
    }


def _owner_state(
    database: Path,
    *,
    benchmark: Any,
    repository: Any,
    schema: Any,
    model_signature: str,
    deadline: int,
) -> dict[str, object]:
    before = benchmark._owner_fences(database)
    with schema.semantic_database(database, readonly=True) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        model = repository._load_model(connection, model_signature)
        pairs = tuple(repository._published_model_generations(connection, (model_signature,)))
    after = benchmark._owner_fences(database)
    if before != after:
        raise BenchError(
            "owner fence changed during guard", phase="owner_guard", code="owner_fence_changed"
        )
    if version != EXPECTED_SCHEMA or not pairs:
        raise BenchError(
            "owner schema/head is not the expected published fixture",
            phase="owner_guard",
            code="owner_binding_invalid",
        )
    deadline_check = time.monotonic_ns()
    if deadline_check > deadline:
        raise DeadlineExceeded("owner_guard")
    return {
        "fences": before,
        "schema_version": version,
        "model": _model_payload(model),
        "published_pairs": [[str(pair[0]), int(pair[1])] for pair in pairs],
    }


def _assert_owner(current: Mapping[str, object], expected: Mapping[str, object]) -> None:
    if _canon(current) != _canon(expected):
        raise BenchError(
            "owner fence/schema/model/head changed",
            phase="owner_guard",
            code="owner_binding_changed",
        )


def _hit_payload(hit: Any) -> dict[str, object]:
    return {
        "ref_id": int(hit.ref_id),
        "entity_id": str(hit.entity_id),
        "item_id": str(hit.item_id),
        "indexed_model_signature": str(hit.indexed_model_signature),
        "vector_space": str(hit.vector_space),
        "modality": str(getattr(hit.modality, "value", hit.modality)),
        "score": float(hit.score),
        "score_hex": float(hit.score).hex(),
        "generation_id": int(hit.generation_id),
        "provenance": dict(hit.provenance),
        "query_model_signature": hit.query_model_signature,
    }


def _page_payload(page: Any) -> dict[str, object]:
    return {
        "hits": [_hit_payload(hit) for hit in page.hits],
        "scanned": int(page.scanned),
        "next_cursor": page.next_cursor,
        "complete": bool(page.complete),
    }


def _resolved_payload(value: Any) -> dict[str, object]:
    return {
        "hit": _hit_payload(value.hit),
        "path": value.path,
        "source_kind": value.source_kind,
        "source_identity": value.source_identity,
        "section_kind": value.section_kind,
        "section_id": value.section_id,
        "start_char": value.start_char,
        "end_char": value.end_char,
        "snippet": value.snippet,
        "source_revision": dict(value.source_revision),
        "section_provenance": dict(value.section_provenance),
        "source_status": value.source_status,
        "published_revision_id": value.published_revision_id,
        "current_revision_id": value.current_revision_id,
    }


def _assert_page(page: Any, size: int, *, phase: str) -> None:
    if (
        page.scanned != size
        or page.complete is not True
        or page.next_cursor is not None
        or len(page.hits) != min(LIMIT, size)
    ):
        raise BenchError("public exact page is incomplete", phase=phase, code="incomplete_page")
    for hit in page.hits:
        if not isinstance(hit.provenance, Mapping):
            raise BenchError(
                "public SearchHit provenance is not a mapping",
                phase=phase,
                code="provenance_missing",
            )


def _close_handle(handle: Any) -> None:
    close = getattr(handle, "close", None)
    if callable(close):
        close()


def _summary(handle: Any, name: str) -> dict[str, object]:
    function = getattr(handle, name, None)
    if not callable(function):
        raise BenchError(
            f"index handle lacks {name}", phase="index_contract", code="handle_summary_missing"
        )
    value = function()
    if not isinstance(value, Mapping):
        raise BenchError(
            f"index {name} has invalid shape", phase="index_contract", code="handle_summary_invalid"
        )
    if name == "usage_summary" and any(
        key not in value
        for key in ("used_queries", "fallback_queries", "rows_scanned", "last_fallback_reason")
    ):
        raise BenchError(
            "index usage summary omits required counters",
            phase="index_contract",
            code="usage_summary_incomplete",
        )
    return json.loads(_canon(dict(value)))


def _trial(
    *,
    name: str,
    route: str,
    resolve: bool,
    size: int,
    database: Path,
    handle: Any | None,
    query: Any,
    expected_owner: Mapping[str, object],
    benchmark: Any,
    repository: Any,
    schema: Any,
    phases: list[dict[str, object]],
    deadline: int,
) -> dict[str, object]:
    phase_start = len(phases)
    started = time.monotonic_ns()

    def guard() -> dict[str, object]:
        state = _owner_state(
            database,
            benchmark=benchmark,
            repository=repository,
            schema=schema,
            model_signature=query.query_model_signature,
            deadline=deadline,
        )
        _assert_owner(state, expected_owner)
        return state

    before = _phase(f"{name}.owner_guard_before", guard, deadline=deadline, phases=phases)

    def search() -> Any:
        kwargs = {
            "limit": LIMIT,
            "max_vectors": size,
            "after_ref_id": 0,
            "batch_size": BATCH,
            "text_scope": "all",
            "cancellation_check": lambda: (
                (_ for _ in ()).throw(DeadlineExceeded(f"{name}.query"))
                if time.monotonic_ns() > deadline
                else None
            ),
        }
        if route == "indexed":
            kwargs["exact_index"] = handle
        return repository.search_exact_page(database, query, **kwargs)

    raw_page = _phase(f"{name}.query", search, deadline=deadline, phases=phases)
    page = _phase(
        f"{name}.public_page",
        lambda: (_assert_page(raw_page, size, phase=f"{name}.public_page"), raw_page)[1],
        deadline=deadline,
        phases=phases,
    )
    resolved = None
    if resolve:
        resolved = _phase(
            f"{name}.resolved",
            lambda: tuple(
                repository.resolve_search_hits(database, page.hits, snippet_chars=240, query=None)
            ),
            deadline=deadline,
            phases=phases,
        )
    after = _phase(f"{name}.owner_guard_after", guard, deadline=deadline, phases=phases)
    _assert_owner(after, expected_owner)
    if _canon(before) != _canon(after):
        raise BenchError(
            "owner changed across public operation",
            phase=f"{name}.owner_guard_after",
            code="owner_changed_across_operation",
        )
    ended = time.monotonic_ns()
    component = sum(float(entry["wall_seconds"]) for entry in phases[phase_start:])
    outer = (ended - started) / 1_000_000_000
    return {
        "route": route,
        "resolve": resolve,
        "page": page,
        "resolved": resolved,
        "comparable_seconds": component,
        "outer_wall_seconds": outer,
        "instrumentation_overhead_seconds": outer - component,
        "phase_names": [str(entry["name"]) for entry in phases[phase_start:]],
    }


def _run_operation(
    *,
    size: int,
    operation: str,
    repeat: int,
    route: str,
    database: Path,
    handle: Any | None,
    query: Any,
    expected_owner: Mapping[str, object],
    benchmark: Any,
    repository: Any,
    schema: Any,
    phases: list[dict[str, object]],
    deadline: int,
) -> dict[str, object]:
    return _trial(
        name=f"size_{size}.warm_{repeat}.{route}.{operation}",
        route=route,
        resolve=operation == "resolved",
        size=size,
        database=database,
        handle=handle,
        query=query,
        expected_owner=expected_owner,
        benchmark=benchmark,
        repository=repository,
        schema=schema,
        phases=phases,
        deadline=deadline,
    )


def _compare(native: Mapping[str, object], indexed: Mapping[str, object]) -> dict[str, object]:
    np, ip = _page_payload(native["page"]), _page_payload(indexed["page"])
    result: dict[str, object] = {
        "native": {
            "comparable_seconds": native["comparable_seconds"],
            "outer_wall_seconds": native["outer_wall_seconds"],
            "instrumentation_overhead_seconds": native["instrumentation_overhead_seconds"],
            "phase_names": native["phase_names"],
            "page_sha256": hashlib.sha256(_canon(np).encode()).hexdigest(),
            "page": np,
        },
        "indexed": {
            "comparable_seconds": indexed["comparable_seconds"],
            "outer_wall_seconds": indexed["outer_wall_seconds"],
            "instrumentation_overhead_seconds": indexed["instrumentation_overhead_seconds"],
            "phase_names": indexed["phase_names"],
            "page_sha256": hashlib.sha256(_canon(ip).encode()).hexdigest(),
            "page": ip,
        },
        "page_equal": _canon(np) == _canon(ip),
    }
    if native["resolved"] is not None or indexed["resolved"] is not None:
        nr = [_resolved_payload(value) for value in (native["resolved"] or ())]
        ir = [_resolved_payload(value) for value in (indexed["resolved"] or ())]
        result["native"]["resolved"] = nr
        result["indexed"]["resolved"] = ir
        result["native"]["resolved_sha256"] = hashlib.sha256(_canon(nr).encode()).hexdigest()
        result["indexed"]["resolved_sha256"] = hashlib.sha256(_canon(ir).encode()).hexdigest()
        result["resolved_equal"] = _canon(nr) == _canon(ir)
    return result


def _p95(values: Sequence[float]) -> dict[str, object]:
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * 0.95
    low, high = math.floor(position), math.ceil(position)
    p95 = (
        ordered[low]
        if low == high
        else ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    )
    return {"samples": len(ordered), "p50": ordered[len(ordered) // 2], "p95": p95}


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--sizes", nargs="+", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if (
        not args.sizes
        or any(value not in SIZES for value in args.sizes)
        or len(set(args.sizes)) != len(args.sizes)
    ):
        parser.error("--sizes must be unique members of 100, 100000, 500000")
    if (
        not 1 <= args.repeats <= MAX_REPEATS
        or not math.isfinite(args.timeout_seconds)
        or not 0 < args.timeout_seconds <= MAX_SECONDS
    ):
        parser.error("--repeats/--timeout-seconds exceed bounded limits")
    return args


def _write_exclusive(path: Path, value: Mapping[str, object]) -> None:
    temp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    raw = (_canon(value) + "\n").encode("utf-8")
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)
        temp.unlink()
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    started = time.monotonic_ns()
    deadline = started + round(args.timeout_seconds * 1_000_000_000)
    phases: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "track": "SEM-A01",
        "status": "failed",
        "scope": {
            "modality": "text",
            "text_scope": "all",
            "published_head": "one selected head",
            "pagination": "after_ref_id=0 exhaustive single page",
            "default_exact_index": None,
            "integration": "opt_in_only_default_off",
        },
        "primary_acceptance": False,
        "model_real": False,
        "model_loaded": False,
        "network_requested": False,
        "constraints": {
            "max_rss_bytes": MAX_RSS,
            "max_temporary_bytes": MAX_TEMP,
            "index_max_total_bytes": INDEX_MAX_BYTES,
            "swap_bytes": 0,
            "math_threads": 1,
        },
        "phases": phases,
        "sizes": results,
        "errors": [],
    }
    run_root: Path | None = None
    restore_guards: Callable[[], None] | None = None
    restore_build: Callable[[], None] | None = None
    output: Path | None = None
    open_handles: list[Any] = []
    try:
        lab_raw = os.environ.get("NEOCORTEX_AUDIT_LAB_ROOT")
        if not lab_raw:
            raise BenchError(
                "NEOCORTEX_AUDIT_LAB_ROOT is required", phase="preflight", code="lab_root_missing"
            )
        lab_root = _real_dir(Path(lab_raw), "lab root")
        repository_root = _real_dir(args.repository_root, "--repository-root")
        output = _new_output(args.output, repository_root)
        run_root = Path(tempfile.mkdtemp(prefix=f"exact-index-{os.getpid()}-", dir=lab_root))
        os.chmod(run_root, 0o700)
        thread_before = {
            name: os.environ.get(name)
            for name in (
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
        }
        env = _private_environment(run_root)
        report["environment"] = {
            "private_home": env["HOME"],
            "threads_before": thread_before,
            "threads_effective": {name: os.environ.get(name) for name in thread_before},
            "quota_ceiling_note": "runner cgroup ceiling is not the math-thread count",
        }
        report["runtime"] = {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
        }
        benchmark_path = repository_root / "benchmarks" / "semantic_exact_search_benchmark.py"
        benchmark_path = _real_dir(benchmark_path.parent, "benchmark parent") / benchmark_path.name
        if not benchmark_path.is_file() or benchmark_path.is_symlink():
            raise BenchError(
                "repo-native benchmark source is unavailable",
                phase="fixture",
                code="benchmark_source_missing",
            )
        fixture: dict[str, Any] = {}

        def load_fixture() -> None:
            nonlocal restore_guards
            _clear_neocortex()
            sys.path.insert(0, str(repository_root))
            loaded_benchmark = _load_module(
                benchmark_path, f"semantic_exact_search_benchmark_{os.getpid()}"
            )
            if (
                int(getattr(loaded_benchmark, "EXPECTED_SEMANTIC_SCHEMA_VERSION", -1))
                != EXPECTED_SCHEMA
            ):
                raise BenchError(
                    "frozen benchmark helper is not SOURCE10",
                    phase="fixture",
                    code="source_schema_version_mismatch",
                )
            loaded_model = loaded_benchmark._fixture_model(str(loaded_benchmark.MODEL_SIGNATURE))
            loaded_seed = int(loaded_benchmark.DEFAULT_SEED)
            loaded_vector = tuple(
                loaded_benchmark._query_vector(loaded_model.dimensions, loaded_seed)
            )
            if (
                loaded_seed != 20260912
                or int(loaded_model.dimensions) != 768
                or str(loaded_model.vector_dtype.value) != "float16"
                or str(loaded_model.modality.value) != "text"
            ):
                raise BenchError(
                    "repo-native fixture contract changed",
                    phase="fixture",
                    code="fixture_contract_mismatch",
                )
            import neocortex

            loaded_repository = importlib.import_module(
                "neocortex.semantic.semantic_search_repository"
            )
            loaded_schema = importlib.import_module("neocortex.semantic.semantic_schema")
            loaded_models = importlib.import_module("neocortex.semantic.semantic_models")
            loaded_exact_index = importlib.import_module("neocortex.semantic.semantic_exact_index")
            if not _inside(Path(str(neocortex.__file__)).resolve(), repository_root):
                raise BenchError(
                    "product import did not resolve from frozen source",
                    phase="fixture",
                    code="source_import_mismatch",
                )
            required = ("prepare_exact_index", "open_exact_index")
            if any(
                not callable(getattr(loaded_exact_index, name, None)) for name in required
            ) or not callable(getattr(loaded_repository, "search_exact_page", None)):
                raise BenchError(
                    "future exact-index API is unavailable",
                    phase="fixture",
                    code="exact_index_api_missing",
                )
            restore_guards = _install_db_guards(deadline)
            fixture.update(
                {
                    "benchmark": loaded_benchmark,
                    "fixture_model": loaded_model,
                    "seed": loaded_seed,
                    "vector": loaded_vector,
                    "repository": loaded_repository,
                    "schema": loaded_schema,
                    "models": loaded_models,
                    "exact_index": loaded_exact_index,
                }
            )
            report["source"] = {
                "repository_root": str(repository_root),
                "benchmark": str(benchmark_path),
                "benchmark_sha256": _sha_file(benchmark_path),
                "semantic_schema_sha256": _sha_file(Path(loaded_schema.__file__).resolve()),
                "search_repository_sha256": _sha_file(Path(loaded_repository.__file__).resolve()),
                "exact_index_sha256": _sha_file(Path(loaded_exact_index.__file__).resolve()),
                "semantic_schema_version": EXPECTED_SCHEMA,
                "query_seed": loaded_seed,
                "query_dimensions": int(loaded_model.dimensions),
            }
            report["model"] = {
                "signature": loaded_model.model_signature,
                "vector_space": loaded_model.vector_space,
                "dimensions": loaded_model.dimensions,
                "dtype": loaded_model.vector_dtype.value,
                "modality": loaded_model.modality.value,
                "real": False,
                "loaded": False,
            }

        _phase("fixture", load_fixture, deadline=deadline, phases=phases)
        benchmark = fixture["benchmark"]
        fixture_model = fixture["fixture_model"]
        seed = fixture["seed"]
        vector = fixture["vector"]
        repository = fixture["repository"]
        schema = fixture["schema"]
        models = fixture["models"]
        exact_index = fixture["exact_index"]
        query = models.ExactSearchQuery(
            query_model_signature=fixture_model.model_signature,
            vector_space=fixture_model.vector_space,
            dimensions=fixture_model.dimensions,
            vector=vector,
            target_modality=models.EmbeddingModality.TEXT,
            indexed_model_signatures=(fixture_model.model_signature,),
        )
        for size in args.sizes:
            if time.monotonic_ns() > deadline:
                raise DeadlineExceeded(f"size_{size}")
            restore_build = _patch_build(benchmark, deadline)
            database = run_root / f"semantic-{size}.sqlite3"
            directory = run_root / f"index-{size}"
            try:
                generation_id = _phase(
                    f"size_{size}.build_fixture",
                    partial(
                        benchmark._build_synthetic_fixture,
                        database,
                        size,
                        model_spec=fixture_model,
                        model_signature=fixture_model.model_signature,
                        dimensions=fixture_model.dimensions,
                        seed=seed,
                    ),
                    deadline=deadline,
                    phases=phases,
                )
            finally:
                restore_build()
                restore_build = None
            _dir_bytes(run_root, deadline)
            owner_call = partial(
                _owner_state,
                database,
                benchmark=benchmark,
                repository=repository,
                schema=schema,
                model_signature=fixture_model.model_signature,
                deadline=deadline,
            )
            owner = _phase(
                f"size_{size}.owner_after_build",
                owner_call,
                deadline=deadline,
                phases=phases,
            )
            _assert_owner_model = _model_payload(fixture_model)
            if _canon(owner["model"]) != _canon(_assert_owner_model):
                raise BenchError(
                    "owner model metadata differs from fixture",
                    phase=f"size_{size}.owner_after_build",
                    code="model_contract_mismatch",
                )
            prepared = _phase(
                f"size_{size}.prepare_index",
                partial(
                    exact_index.prepare_exact_index,
                    database,
                    directory,
                    model_signature=fixture_model.model_signature,
                    text_scope="all",
                    max_rows=size,
                    max_total_bytes=INDEX_MAX_BYTES,
                    cancellation_check=partial(
                        _deadline_callback, deadline, f"size_{size}.prepare_index"
                    ),
                ),
                deadline=deadline,
                phases=phases,
            )
            prepared_summary, prepared_usage = (
                _summary(prepared, "summary"),
                _summary(prepared, "usage_summary"),
            )
            _close_handle(prepared)
            _dir_bytes(run_root, deadline)
            owner_after_prepare = _phase(
                f"size_{size}.owner_after_prepare",
                owner_call,
                deadline=deadline,
                phases=phases,
            )
            _assert_owner(owner_after_prepare, owner)
            handle = _phase(
                f"size_{size}.open_index_verify",
                partial(
                    exact_index.open_exact_index,
                    database,
                    directory,
                    max_rows=size,
                    max_total_bytes=INDEX_MAX_BYTES,
                    cancellation_check=partial(
                        _deadline_callback, deadline, f"size_{size}.open_index_verify"
                    ),
                ),
                deadline=deadline,
                phases=phases,
            )
            open_handles.append(handle)
            opened_summary, opened_usage = (
                _summary(handle, "summary"),
                _summary(handle, "usage_summary"),
            )
            cold_native = _trial(
                name=f"size_{size}.cold.native.page",
                route="native",
                resolve=False,
                size=size,
                database=database,
                handle=None,
                query=query,
                expected_owner=owner,
                benchmark=benchmark,
                repository=repository,
                schema=schema,
                phases=phases,
                deadline=deadline,
            )
            cold_indexed = _trial(
                name=f"size_{size}.cold.indexed.page",
                route="indexed",
                resolve=False,
                size=size,
                database=database,
                handle=handle,
                query=query,
                expected_owner=owner,
                benchmark=benchmark,
                repository=repository,
                schema=schema,
                phases=phases,
                deadline=deadline,
            )
            page_trials: list[dict[str, object]] = []
            resolved_trials: list[dict[str, object]] = []
            for repeat in range(1, args.repeats + 1):
                order = ("native", "indexed") if repeat % 2 else ("indexed", "native")
                collected: dict[str, dict[str, object]] = {}
                for route in order:
                    collected[route] = _run_operation(
                        size=size,
                        operation="page",
                        repeat=repeat,
                        route=route,
                        database=database,
                        handle=handle if route == "indexed" else None,
                        query=query,
                        expected_owner=owner,
                        benchmark=benchmark,
                        repository=repository,
                        schema=schema,
                        phases=phases,
                        deadline=deadline,
                    )
                page_trials.append(_compare(collected["native"], collected["indexed"]))
                collected = {}
                for route in order:
                    collected[route] = _run_operation(
                        size=size,
                        operation="resolved",
                        repeat=repeat,
                        route=route,
                        database=database,
                        handle=handle if route == "indexed" else None,
                        query=query,
                        expected_owner=owner,
                        benchmark=benchmark,
                        repository=repository,
                        schema=schema,
                        phases=phases,
                        deadline=deadline,
                    )
                resolved_trials.append(_compare(collected["native"], collected["indexed"]))
            usage = _summary(handle, "usage_summary")
            used = int(usage.get("used_queries", 0))
            fallbacks = int(usage.get("fallback_queries", 0))
            expected_calls = 1 + 2 * args.repeats
            if (
                fallbacks != 0
                or usage.get("last_fallback_reason") not in (None, "")
                or used < expected_calls
            ):
                raise BenchError(
                    "indexed usage counters do not prove indexed warm calls",
                    phase=f"size_{size}.usage",
                    code="indexed_usage_invalid",
                )
            cold_compare = _compare(cold_native, cold_indexed)
            page_equal = all(trial["page_equal"] for trial in page_trials) and bool(
                cold_compare["page_equal"]
            )
            resolved_equal = all(
                trial.get("resolved_equal") is True and trial["page_equal"]
                for trial in resolved_trials
            )
            result = {
                "size": size,
                "generation_id": int(generation_id),
                "acceptance_scope": "canary_non_baseline_non_primary"
                if size == 100
                else "normal_baseline_primary_comparable",
                "status": "complete" if page_equal and resolved_equal else "failed",
                "fixture_database_bytes": _dir_bytes(run_root, deadline),
                "index_directory": str(directory),
                "prepare_summary": prepared_summary,
                "prepare_usage": prepared_usage,
                "open_summary": opened_summary,
                "open_usage": opened_usage,
                "final_usage": usage,
                "usage_expected_indexed_calls": expected_calls,
                "cold_page": cold_compare,
                "page_trials": page_trials,
                "resolved_trials": resolved_trials,
                "latency": {
                    "page": {
                        "native": _p95(
                            [float(v["native"]["comparable_seconds"]) for v in page_trials]
                        ),
                        "indexed": _p95(
                            [float(v["indexed"]["comparable_seconds"]) for v in page_trials]
                        ),
                        "metric": "comparable_seconds",
                        "primary": True,
                    },
                    "resolved": {
                        "native": _p95(
                            [float(v["native"]["comparable_seconds"]) for v in resolved_trials]
                        ),
                        "indexed": _p95(
                            [float(v["indexed"]["comparable_seconds"]) for v in resolved_trials]
                        ),
                        "metric": "comparable_seconds",
                        "primary": False,
                    },
                },
                "equivalence": {
                    "page_equal": page_equal,
                    "resolved_equal": resolved_equal,
                    "fields": [
                        "Page.hits",
                        "Page.scanned",
                        "Page.next_cursor",
                        "Page.complete",
                        "SearchHit.ref_id",
                        "entity_id",
                        "item_id",
                        "indexed_model_signature",
                        "vector_space",
                        "modality",
                        "score",
                        "score_hex",
                        "generation_id",
                        "provenance",
                        "query_model_signature",
                        "ResolvedSearchHit.path",
                        "source_kind",
                        "source_identity",
                        "section_kind",
                        "section_id",
                        "start_char",
                        "end_char",
                        "snippet",
                        "source_revision",
                        "section_provenance",
                        "source_status",
                        "published_revision_id",
                        "current_revision_id",
                    ],
                },
            }
            results.append(result)
            _close_handle(handle)
            open_handles.remove(handle)
        report.update(
            {
                "status": "complete"
                if all(result["status"] == "complete" for result in results)
                else "failed",
                "primary_acceptance": all(
                    size != 100 and result["status"] == "complete"
                    for size, result in zip(args.sizes, results, strict=True)
                ),
                "elapsed_seconds": (time.monotonic_ns() - started) / 1_000_000_000,
            }
        )
    except BaseException as exc:
        report["errors"].append(_error(exc))
        report["status"] = "failed"
        report["primary_acceptance"] = False
    finally:
        if restore_build is not None:
            restore_build()
        for handle in reversed(open_handles):
            try:
                _close_handle(handle)
            except BaseException as exc:
                report["errors"].append(_error(exc, "cleanup"))
                report["status"] = "failed"
        if restore_guards is not None:
            restore_guards()
        if run_root is not None:
            try:
                shutil.rmtree(run_root)
            except OSError as exc:
                report["errors"].append(_error(exc, "cleanup"))
                report["status"] = "failed"
        report["elapsed_seconds"] = (time.monotonic_ns() - started) / 1_000_000_000
        report["deadline_seconds"] = args.timeout_seconds
        report["deadline_met"] = report["elapsed_seconds"] <= args.timeout_seconds
        report["process_peak"] = {
            "rss_peak_bytes": max(
                (int(value.get("rss_peak_bytes") or 0) for value in phases), default=0
            ),
            "vms_peak_bytes": max(
                (int(value.get("vms_peak_bytes") or 0) for value in phases), default=0
            ),
            "threads_peak": max(
                (int(value.get("peak_threads") or 0) for value in phases), default=0
            ),
        }
        if report["deadline_met"] is not True:
            report["status"] = "failed"
            report["errors"].append(
                {
                    "type": "DeadlineExceeded",
                    "code": "deadline_exceeded",
                    "phase": "total",
                    "message": "total deadline exceeded",
                }
            )
        if report["status"] != "complete":
            report["primary_acceptance"] = False
    if output is None:
        print(_canon(report))
        return 1
    try:
        _write_exclusive(output, report)
    except BaseException as exc:
        print(
            _canon({"schema": REPORT_SCHEMA, "status": "failed", "error": _error(exc, "report")}),
            file=sys.stderr,
        )
        return 2
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
