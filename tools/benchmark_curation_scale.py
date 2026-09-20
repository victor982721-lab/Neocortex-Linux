"""Opt-in benchmark for bounded inventory throughput over synthetic fixtures.

The benchmark never selects the user's corpus.  By default it creates a small
temporary tree and removes it before returning.  Counts above 100,000 require
``--allow-large`` explicitly, and a receipt is written only when the caller
passes an external ``--receipt`` path.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import math
import os
import shutil
import stat
import sys
import tempfile
import time
import resource
import tracemalloc
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from neocortex.deduplication import DedupIndex, InventoryCheckpoint, ScanSummary  # noqa: E402
from neocortex.deduplication.inventory.scanner import InventoryBatch  # noqa: E402


BENCHMARK_SCHEMA = "neocortex.curation-scale-benchmark/v1"
DEFAULT_COUNT = 256
DEFAULT_PAYLOAD_BYTES = 32
DEFAULT_BATCH_SIZE = 512
DEFAULT_TIMEOUT_SECONDS = 600.0
LARGE_COUNT_THRESHOLD = 100_000
MAX_COUNT = 1_000_000
MAX_BATCH_SIZE = 10_000
MAX_PAYLOAD_BYTES = 1_024
MAX_TIMEOUT_SECONDS = 900.0
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MIN_FREE_SPACE_BYTES = 64 * 1024 * 1024


class BenchmarkConfigurationError(ValueError):
    """The benchmark request is outside its explicit bounded contract."""


class BenchmarkTimeout(RuntimeError):
    """The synthetic run reached its caller-provided hard time budget."""


@dataclass(frozen=True, slots=True)
class FixtureEntry:
    """One deterministic, bounded fixture file specification."""

    index: int
    relative_path: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class FixtureGeneration:
    """Summary of one temporary fixture generation pass."""

    files: int
    bytes: int
    elapsed_seconds: float
    digest: str
    directories: int


@dataclass(frozen=True, slots=True)
class _BenchmarkConfig:
    count: int
    payload_bytes: int
    batch_size: int
    timeout_seconds: float
    allow_large: bool

    @property
    def estimated_bytes(self) -> int:
        return self.count * self.payload_bytes


def _positive_integer(value: object, *, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise BenchmarkConfigurationError(f"{label} must be between 1 and {maximum}")
    return value


def _timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkConfigurationError("timeout_seconds must be a real number")
    result = float(value)
    if not math.isfinite(result) or not 1.0 <= result <= MAX_TIMEOUT_SECONDS:
        raise BenchmarkConfigurationError(
            f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS:g}"
        )
    return result


def validate_config(
    *,
    count: int,
    payload_bytes: int = DEFAULT_PAYLOAD_BYTES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    allow_large: bool = False,
) -> _BenchmarkConfig:
    """Validate a bounded benchmark request before creating any file."""

    bounded_count = _positive_integer(count, label="count", maximum=MAX_COUNT)
    bounded_payload = _positive_integer(
        payload_bytes,
        label="payload_bytes",
        maximum=MAX_PAYLOAD_BYTES,
    )
    bounded_batch = _positive_integer(
        batch_size,
        label="batch_size",
        maximum=MAX_BATCH_SIZE,
    )
    bounded_timeout = _timeout(timeout_seconds)
    if bounded_count > LARGE_COUNT_THRESHOLD and not allow_large:
        raise BenchmarkConfigurationError("count above 100000 requires explicit --allow-large")
    estimated_bytes = bounded_count * bounded_payload
    if estimated_bytes > MAX_TOTAL_BYTES:
        raise BenchmarkConfigurationError(
            f"estimated fixture bytes exceed the hard cap of {MAX_TOTAL_BYTES}"
        )
    return _BenchmarkConfig(
        bounded_count,
        bounded_payload,
        bounded_batch,
        bounded_timeout,
        bool(allow_large),
    )


def _payload(index: int, size: int) -> bytes:
    seed = f"neocortex-scale-fixture-v1:{index:010d}".encode("ascii")
    repeated = (seed * ((size + len(seed) - 1) // len(seed)))[:size]
    return repeated


def iter_fixture_entries(
    count: int,
    *,
    payload_bytes: int = DEFAULT_PAYLOAD_BYTES,
) -> Iterator[FixtureEntry]:
    """Yield deterministic entries one at a time without retaining the tree."""

    bounded_count = _positive_integer(count, label="count", maximum=MAX_COUNT)
    bounded_payload = _positive_integer(
        payload_bytes,
        label="payload_bytes",
        maximum=MAX_PAYLOAD_BYTES,
    )
    for index in range(bounded_count):
        shard = index // 1_000
        yield FixtureEntry(
            index=index,
            relative_path=f"shard-{shard:04d}/item-{index:07d}.bin",
            payload=_payload(index, bounded_payload),
        )


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise BenchmarkTimeout("synthetic benchmark exceeded its time budget")


def generate_fixture(
    root: Path,
    count: int,
    *,
    payload_bytes: int = DEFAULT_PAYLOAD_BYTES,
    deadline: float | None = None,
) -> FixtureGeneration:
    """Create one temporary deterministic tree while retaining only one payload."""

    root = Path(root)
    if not root.is_absolute():
        raise BenchmarkConfigurationError("fixture root must be absolute")
    # The generator is intentionally incapable of selecting a user corpus:
    # every caller-supplied destination must be a new path below the host
    # temporary directory, and every pre-existing ancestor is checked with
    # ``lstat`` before any mkdir follows it.
    temporary_root = Path(tempfile.gettempdir()).resolve()
    try:
        root.parent.resolve(strict=False).relative_to(temporary_root)
    except ValueError as exc:
        raise BenchmarkConfigurationError(
            "fixture root must be below the system temporary directory"
        ) from exc
    missing: list[Path] = []
    current = root.parent
    while not current.exists():
        if os.path.lexists(current):
            raise BenchmarkConfigurationError("fixture root parent is a dangling link")
        missing.append(current)
        if current == current.parent:
            raise BenchmarkConfigurationError("fixture root has no stable parent")
        current = current.parent
    while True:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise BenchmarkConfigurationError("fixture root parent contains an unsafe component")
        if current == temporary_root or current == current.parent:
            break
        current = current.parent
    if os.path.lexists(root):
        raise BenchmarkConfigurationError("fixture root must not already exist")
    bounded_count = _positive_integer(count, label="count", maximum=MAX_COUNT)
    bounded_payload = _positive_integer(
        payload_bytes,
        label="payload_bytes",
        maximum=MAX_PAYLOAD_BYTES,
    )
    started = time.perf_counter()
    digest = hashlib.sha256()
    files = bytes_written = directories = 0
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    for entry in iter_fixture_entries(bounded_count, payload_bytes=bounded_payload):
        _check_deadline(deadline)
        if bytes_written + len(entry.payload) > MAX_TOTAL_BYTES:
            raise BenchmarkConfigurationError(
                f"generated fixture bytes exceed the hard cap of {MAX_TOTAL_BYTES}"
            )
        path = root / entry.relative_path
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700)
            directories += 1
        path.write_bytes(entry.payload)
        encoded_path = entry.relative_path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(entry.payload).to_bytes(8, "big"))
        digest.update(entry.payload)
        files += 1
        bytes_written += len(entry.payload)
    return FixtureGeneration(
        files=files,
        bytes=bytes_written,
        elapsed_seconds=time.perf_counter() - started,
        digest=digest.hexdigest(),
        directories=directories,
    )


def _rate(count: int, elapsed_seconds: float) -> float:
    return float(count) / elapsed_seconds if elapsed_seconds > 0 else 0.0


def _rss_kib() -> int | None:
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (AttributeError, OSError, ValueError):
        return None
    return value if value >= 0 else None


def _database_bytes(database: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for label, path in (
        ("database", database),
        ("wal", Path(f"{database}-wal")),
        ("shm", Path(f"{database}-shm")),
    ):
        try:
            result[label] = path.stat().st_size
        except FileNotFoundError:
            result[label] = 0
    return result


def _scan_with_metrics(
    database: Path,
    corpus: Path,
    *,
    batch_size: int,
    deadline: float,
) -> tuple[ScanSummary, dict[str, object]]:
    flush_count = 0
    max_pending_rows = 0
    progress_events = 0
    original_flush = InventoryBatch.flush

    def observed_flush(
        batch: InventoryBatch,
        *,
        delegate: Callable[[InventoryBatch], None] = original_flush,
    ) -> None:
        nonlocal flush_count, max_pending_rows
        _check_deadline(deadline)
        pending = len(batch._rows)
        if pending:
            flush_count += 1
            max_pending_rows = max(max_pending_rows, pending)
        delegate(batch)
        _check_deadline(deadline)

    def progress(event: object) -> None:
        nonlocal progress_events
        progress_events += 1
        _check_deadline(deadline)

    started = time.perf_counter()
    with patch.object(InventoryBatch, "flush", observed_flush):
        with DedupIndex(database) as index:
            summary = index.scan(
                corpus,
                batch_size=batch_size,
                excluded_paths=(),
                progress=progress,
            )
            index.bind_inventory_checkpoint(
                InventoryCheckpoint(str(corpus), summary.scan_id)
            )
            _check_deadline(deadline)
    elapsed = time.perf_counter() - started
    return summary, {
        "elapsed_seconds": elapsed,
        "files_per_second": _rate(summary.files_seen, elapsed),
        "bytes_per_second": _rate(summary.bytes_seen, elapsed),
        "eta_seconds": 0.0,
        "batches": flush_count,
        "commits": flush_count,
        "max_pending_rows": max_pending_rows,
        "progress_events": progress_events,
        "scan_id": summary.scan_id,
    }


def _memory_snapshot() -> tuple[bool, int | None]:
    if not tracemalloc.is_tracing():
        return False, None
    _current, peak = tracemalloc.get_traced_memory()
    return True, int(peak)


def _receipt_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise BenchmarkConfigurationError("receipt path must be absolute")
    resolved = Path(os.path.realpath(candidate))
    repository = Path(os.path.realpath(REPOSITORY_ROOT))
    try:
        resolved.relative_to(repository)
    except ValueError:
        pass
    else:
        raise BenchmarkConfigurationError("receipt path must be outside the repository")
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise BenchmarkConfigurationError("receipt path cannot be inspected") from exc
    if metadata is not None:
        raise BenchmarkConfigurationError("receipt path already exists")
    parent = candidate.parent
    try:
        parent_metadata = os.lstat(parent)
    except FileNotFoundError as exc:
        raise BenchmarkConfigurationError("receipt parent directory must already exist") from exc
    except OSError as exc:
        raise BenchmarkConfigurationError("receipt parent directory cannot be inspected") from exc
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise BenchmarkConfigurationError("receipt parent directory must be a real directory")
    if Path(os.path.realpath(parent)) != Path(os.path.abspath(parent)):
        raise BenchmarkConfigurationError("receipt parent directory must not traverse a symlink")
    return candidate


def _write_receipt(path: Path, report: dict[str, object]) -> None:
    payload = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    parent = path.parent
    directory_fd: int | None = None
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        directory_fd = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _rename_noreplace(temporary_path, path, directory_fd)
        temporary_path = None
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)


def _rename_noreplace(source: Path, destination: Path, directory_fd: int) -> None:
    """Atomically publish a same-directory file without replacing a target."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BenchmarkConfigurationError("atomic receipt rename is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            directory_fd,
            os.fsencode(source.name),
            directory_fd,
            os.fsencode(destination.name),
            1,
        )
        == 0
    ):
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise BenchmarkConfigurationError("receipt path already exists")
    raise OSError(error_number, os.strerror(error_number))


def _assert_free_space(parent: Path, estimated_bytes: int) -> None:
    try:
        free_bytes = int(shutil.disk_usage(parent).free)
    except OSError as exc:
        raise BenchmarkConfigurationError(
            "temporary fixture free space cannot be inspected"
        ) from exc
    required = estimated_bytes + MIN_FREE_SPACE_BYTES
    if free_bytes < required:
        raise BenchmarkConfigurationError(
            f"temporary fixture requires {required} bytes of free space; only {free_bytes} available"
        )


def run_benchmark(
    *,
    count: int = DEFAULT_COUNT,
    payload_bytes: int = DEFAULT_PAYLOAD_BYTES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    allow_large: bool = False,
    receipt_path: str | Path | None = None,
) -> dict[str, object]:
    """Run the benchmark in an ephemeral tree and return one bounded report."""

    config = validate_config(
        count=count,
        payload_bytes=payload_bytes,
        batch_size=batch_size,
        timeout_seconds=timeout_seconds,
        allow_large=allow_large,
    )
    external_receipt = _receipt_path(receipt_path)
    started = time.perf_counter()
    deadline = started + config.timeout_seconds
    memory_available = False
    generation: FixtureGeneration | None = None
    scan_summary: ScanSummary | None = None
    scan_metrics: dict[str, object] = {}
    error: str | None = None
    try:
        tracemalloc.start()
        with TemporaryDirectory(prefix="neocortex-curation-scale-") as temporary:
            _assert_free_space(Path(temporary), config.estimated_bytes)
            root = Path(temporary) / "corpus"
            generation = generate_fixture(
                root,
                config.count,
                payload_bytes=config.payload_bytes,
                deadline=deadline,
            )
            memory_available, generation_peak = _memory_snapshot()
            if memory_available:
                tracemalloc.reset_peak()
            scan_summary, scan_metrics = _scan_with_metrics(
                Path(temporary) / "dedup.sqlite3",
                root,
                batch_size=config.batch_size,
                deadline=deadline,
            )
            _check_deadline(deadline)
            memory_available, scan_peak = _memory_snapshot()
            database_bytes = _database_bytes(Path(temporary) / "dedup.sqlite3")
    except BenchmarkTimeout as exc:
        error = str(exc)
        generation_peak = None
        scan_peak = None
        database_bytes = {}
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()

    elapsed = time.perf_counter() - started
    if generation is None or scan_summary is None:
        status = "timeout" if error is not None else "failed"
        report: dict[str, object] = {
            "schema": BENCHMARK_SCHEMA,
            "status": status,
            "error": error or "benchmark did not produce a complete report",
            "elapsed_seconds": elapsed,
            "fixture": {
                "count_requested": config.count,
                "allow_large": config.allow_large,
                "temporary": True,
            },
            "receipt_written": False,
        }
    else:
        generation_rate = _rate(generation.files, generation.elapsed_seconds)
        report = {
            "schema": BENCHMARK_SCHEMA,
            "status": "complete",
            "elapsed_seconds": elapsed,
            "fixture": {
                "count": generation.files,
                "bytes": generation.bytes,
                "payload_bytes": config.payload_bytes,
                "directories": generation.directories,
                "digest_sha256": generation.digest,
                "temporary": True,
            },
            "generation": {
                "elapsed_seconds": generation.elapsed_seconds,
                "files_per_second": generation_rate,
                "bytes_per_second": _rate(generation.bytes, generation.elapsed_seconds),
                "eta_seconds": 0.0,
            },
            "scan": {
                "files": scan_summary.files_seen,
                "bytes": scan_summary.bytes_seen,
                "directories": scan_summary.directories_seen,
                "skipped_links": scan_summary.skipped_links,
                "errors": scan_summary.errors,
                **scan_metrics,
            },
            "batches": {
                "batch_size": config.batch_size,
                "commits": scan_metrics.get("commits", 0),
                "max_pending_rows": scan_metrics.get("max_pending_rows", 0),
            },
            "memory": {
                "available": memory_available,
                "generation_peak_python_heap_bytes": generation_peak,
                "scan_peak_python_heap_bytes": scan_peak,
                "max_rss_kib": _rss_kib(),
            },
            "storage": {"database_bytes": database_bytes},
            "receipt_written": external_receipt is not None,
        }

    if external_receipt is not None:
        _write_receipt(external_receipt, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--payload-bytes", type=int, default=DEFAULT_PAYLOAD_BYTES)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--allow-large",
        action="store_true",
        help="explicitly allow generation above 100000 temporary files",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        help="write one receipt JSON to this absolute path outside the repository",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    parsed = parser.parse_args(arguments)
    try:
        report = run_benchmark(
            count=parsed.count,
            payload_bytes=parsed.payload_bytes,
            batch_size=parsed.batch_size,
            timeout_seconds=parsed.timeout_seconds,
            allow_large=parsed.allow_large,
            receipt_path=parsed.receipt,
        )
    except (BenchmarkConfigurationError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("status") == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "BENCHMARK_SCHEMA",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_COUNT",
    "DEFAULT_PAYLOAD_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "BenchmarkConfigurationError",
    "BenchmarkTimeout",
    "FixtureEntry",
    "FixtureGeneration",
    "generate_fixture",
    "iter_fixture_entries",
    "main",
    "run_benchmark",
    "validate_config",
)
