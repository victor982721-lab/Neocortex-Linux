#!/usr/bin/env python3
"""Bounded Semantic receipt/outbox storage benchmark.

The harness calls the repository's real ``_record_work_receipt`` writer and
``read_semantic_derivation_outbox`` reader over an isolated synthetic owner.
It intentionally does not run the embedding pipeline, load a model, read a
corpus, or import anything from an audit tree.  A point is complete only when
all requested receipts are written, all are hydrated through the reader, the
cursor is exhausted, and the bounded projection replay is equivalent.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import resource
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
BENCHMARK_SCHEMA = "neocortex.semantic-receipt-outbox-storage-benchmark/v1"
RECEIPT_CONTRACT = "neocortex.work-receipt/v1"
WIRE_V1 = "neocortex.semantic-derivation-event/v1"
WIRE_V2 = "neocortex.semantic-derivation-event/v2"
NORMAL_COUNTS = (100, 1_000, 5_000)
MAX_RECEIPTS = 5_000
DEFAULT_PAYLOAD_BYTES = 0
MAX_PAYLOAD_TARGET_BYTES = 900_000
MAX_RECEIPT_BYTES = 1_000_000
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_READER_BYTES = 256 * 1024 * 1024
MAX_PROJECTION_BYTES = 64 * 1024 * 1024
PAGE_LIMIT = 1_000
SQL_VM_INTERVAL = 1_000
BASE_TIME_NS = 1_700_000_000_000_000_000
STAGE_ID = "semantic.a04.receipt.outbox.benchmark"
STAGE_VERSION = "semantic-a04-receipt-outbox-benchmark-v1"
PROCESSING_SIGNATURE = "semantic-a04-receipt-outbox-benchmark-v1"
EXPECTED_SCHEMA_TO_WIRE = {8: WIRE_V1, 9: WIRE_V2}
KNOWN_SEMANTIC_SCHEMAS = frozenset(EXPECTED_SCHEMA_TO_WIRE)
STRESS_BINDING_COUNT = 200
MAX_LARGE_PAYLOAD_COUNT = 10
MAX_ESTIMATED_TOTAL_RECEIPT_BYTES = 256 * 1024 * 1024


class BenchmarkConfigurationError(ValueError):
    """The requested storage point is outside its bounded contract."""


class BenchmarkExecutionError(RuntimeError):
    """A fixture writer/reader or integrity gate did not complete."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    repository_root: Path | None,
) -> Path:
    if not path.is_absolute():
        raise BenchmarkConfigurationError(f"{label} must be absolute")
    selected = path.expanduser().resolve()
    if repository_root is not None:
        repository = repository_root.expanduser().resolve()
        if _path_is_within(selected, repository) or _path_is_within(repository, selected):
            raise BenchmarkConfigurationError(f"{label} must not overlap the checkout")
    home = Path.home().resolve()
    product_roots = (
        home / ".config" / "Neocortex",
        home / ".local" / "share" / "Neocortex",
        home / ".local" / "state" / "Neocortex",
        home / ".cache" / "Neocortex",
    )
    if any(_path_is_within(selected, root) or _path_is_within(root, selected) for root in product_roots):
        raise BenchmarkConfigurationError(f"{label} must not overlap installed NeoCortex state")
    return selected


def _private_environment(
    root: Path,
    *,
    inherited_boundary: Path | None = None,
) -> dict[str, object]:
    marker_before = os.environ.get("NEOCORTEX_AUDIT_LAB_ROOT")
    resolved_root = root.resolve()
    if inherited_boundary is not None and not _path_is_within(
        resolved_root,
        inherited_boundary.resolve(),
    ):
        raise BenchmarkConfigurationError(
            "run root escapes inherited NEOCORTEX_AUDIT_LAB_ROOT"
        )
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
    environment_names = tuple({**directories, **cache_directories})
    environment_before = {
        name: os.environ.get(name) for name in environment_names
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in cache_directories.values():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {name: str(directory) for name, directory in directories.items()}
    environment.update(
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
    # Keep NEOCORTEX_AUDIT_LAB_ROOT inherited.  It is a marker, not a source
    # or destination authority, and removing it would erase a caller gate.
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "NEOCORTEX_CORPUS_ROOT",
        "NEOCORTEX_TEST_PYTHON",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
    ):
        os.environ.pop(name, None)
    os.environ.update(environment)

    observed: dict[str, dict[str, object]] = {}
    all_private_paths = {**directories, **cache_directories}
    for name, directory in all_private_paths.items():
        resolved = directory.resolve()
        if not _path_is_within(resolved, resolved_root):
            raise BenchmarkConfigurationError(f"private environment escaped run root: {name}")
        if not resolved.is_dir():
            raise BenchmarkConfigurationError(f"private environment path is not a directory: {name}")
        mode = resolved.stat().st_mode & 0o777
        if mode & 0o077:
            raise BenchmarkConfigurationError(
                f"private environment directory is not owner-only: {name} mode={mode:o}"
            )
        actual_value = os.environ.get(name)
        if actual_value is None or Path(actual_value).expanduser().resolve() != resolved:
            raise BenchmarkConfigurationError(f"environment variable was not isolated: {name}")
        observed[name] = {
            "before_present": environment_before[name] is not None,
            "path": actual_value,
            "under_run_root": True,
            "owner_only": True,
            "mode": mode,
        }

    marker_after = os.environ.get("NEOCORTEX_AUDIT_LAB_ROOT")
    marker_preserved = marker_before == marker_after
    if not marker_preserved:
        raise BenchmarkConfigurationError("NEOCORTEX_AUDIT_LAB_ROOT marker changed")
    return {
        "marker_present_before": marker_before is not None,
        "marker_present_after": marker_after is not None,
        "marker_preserved": marker_preserved,
        "variables": observed,
        "all_paths_under_run_root": all(
            bool(item.get("under_run_root")) for item in observed.values()
        ),
    }


def _output_path(path: Path | None, *, repository_root: Path, temp_root: Path) -> Path | None:
    if path is None or str(path) == "-":
        return None
    if path.is_symlink():
        raise BenchmarkConfigurationError(f"refusing a symlink output: {path}")
    selected = _safe_path(path, label="--output", repository_root=repository_root)
    if _path_is_within(selected, temp_root):
        raise BenchmarkConfigurationError("--output must be outside --temp-root")
    if selected.exists() or selected.is_symlink():
        raise BenchmarkConfigurationError(f"refusing to overwrite output: {selected}")
    selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return selected


def _write_report(path: Path | None, value: Mapping[str, object]) -> None:
    encoded = _canonical_json(value)
    if path is None:
        print(encoded)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise BenchmarkConfigurationError(f"temporary report path already exists: {temporary}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _fence(path: Path) -> dict[str, object]:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return {"status": "absent"}
    if path.is_symlink():
        return {"status": "symlink"}
    if not path.is_file():
        return {"status": "non_regular"}
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as error:
        raise BenchmarkExecutionError(f"cannot hash owner file {path}: {error}") from error
    return {
        "status": "present",
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "ctime_ns": int(stat_result.st_ctime_ns),
        "mode": int(stat_result.st_mode & 0o777),
        "sha256": digest.hexdigest(),
    }


def _owner_files(database: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for name, path in (
        ("database", database),
        ("wal", Path(f"{database}-wal")),
        ("shm", Path(f"{database}-shm")),
        ("journal", Path(f"{database}-journal")),
    ):
        try:
            result[name] = int(path.stat().st_size)
        except FileNotFoundError:
            result[name] = 0
    result["total"] = sum(result.values())
    return result


def _owner_fences(database: Path) -> dict[str, dict[str, object]]:
    return {
        "database": _fence(database),
        "wal": _fence(Path(f"{database}-wal")),
        "shm": _fence(Path(f"{database}-shm")),
        "journal": _fence(Path(f"{database}-journal")),
    }


def _read_proc_io() -> dict[str, int] | None:
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


def _read_proc_status() -> dict[str, int] | None:
    try:
        text = Path("/proc/self/status").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    result: dict[str, int] = {}
    for key in ("VmRSS", "VmHWM", "VmSize", "Threads"):
        for line in text.splitlines():
            name, separator, raw = line.partition(":")
            if name != key or not separator:
                continue
            token = raw.strip().split(" ", 1)[0]
            try:
                result[key] = int(token) * (1024 if key != "Threads" else 1)
            except ValueError:
                pass
    return result


class _ProcessSampler:
    def __init__(self, interval_seconds: float = 0.05) -> None:
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak_rss_bytes = 0
        self.peak_vms_bytes = 0
        self.peak_threads = 0

    def _sample(self) -> None:
        status = _read_proc_status()
        if status is None:
            return
        self.peak_rss_bytes = max(self.peak_rss_bytes, status.get("VmRSS", 0))
        self.peak_vms_bytes = max(self.peak_vms_bytes, status.get("VmSize", 0))
        self.peak_threads = max(self.peak_threads, status.get("Threads", 0))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def start(self) -> None:
        self._sample()
        self._thread = threading.Thread(target=self._run, name="semantic-a04-rss", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._sample()


def _rusage() -> dict[str, float]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "user_seconds": float(usage.ru_utime),
        "system_seconds": float(usage.ru_stime),
    }


class _SQLTrace:
    def __init__(self) -> None:
        self.total = 0
        self.reads = 0
        self.writes = 0
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0
        self.progress_callbacks = 0
        self.statement_kinds: Counter[str] = Counter()

    def observe(self, statement: str) -> None:
        verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "EMPTY"
        if verb == "EMPTY":
            return
        self.total += 1
        self.statement_kinds[verb] += 1
        if verb in {"SELECT", "WITH", "PRAGMA", "EXPLAIN"}:
            self.reads += 1
        elif verb in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP"}:
            self.writes += 1
        elif verb == "BEGIN":
            self.begins += 1
        elif verb == "COMMIT":
            self.commits += 1
        elif verb == "ROLLBACK":
            self.rollbacks += 1

    def progress(self) -> int:
        self.progress_callbacks += 1
        return 0

    def payload(self) -> dict[str, object]:
        return {
            "total_statements": self.total,
            "read_statements": self.reads,
            "write_statements": self.writes,
            "begin_statements": self.begins,
            "commit_statements": self.commits,
            "rollback_statements": self.rollbacks,
            "transactions_closed": self.commits + self.rollbacks,
            "progress_callbacks": self.progress_callbacks,
            "vm_steps_lower_bound": self.progress_callbacks * SQL_VM_INTERVAL,
            "progress_interval": SQL_VM_INTERVAL,
            "statement_kinds": dict(sorted(self.statement_kinds.items())),
        }


def _sql_report(trace: _SQLTrace | None) -> dict[str, object]:
    if trace is None:
        return {
            "status": "disabled",
            "reason": "primary_measurement_untraced",
        }
    payload = trace.payload()
    payload.update(
        {
            "status": "diagnostics_canary",
            "reason": "explicit_bounded_canary",
        }
    )
    return payload


@contextlib.contextmanager
def _phase_metrics() -> Iterator[dict[str, object]]:
    before_usage = _rusage()
    before_io = _read_proc_io()
    before_status = _read_proc_status() or {}
    sampler = _ProcessSampler()
    sampler.start()
    started = time.perf_counter_ns()
    result: dict[str, object] = {}
    try:
        yield result
    finally:
        elapsed_ns = time.perf_counter_ns() - started
        sampler.stop()
        after_usage = _rusage()
        after_io = _read_proc_io()
        after_status = _read_proc_status() or {}
        result.update(
            {
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
                "rss_peak_bytes": sampler.peak_rss_bytes,
                "rss_delta_peak_bytes": max(
                    0,
                    sampler.peak_rss_bytes - before_status.get("VmRSS", 0),
                ),
                "vms_peak_bytes": sampler.peak_vms_bytes,
                "peak_threads": sampler.peak_threads,
                "process_io": (
                    None
                    if before_io is None or after_io is None
                    else {
                        "before": before_io,
                        "after": after_io,
                        "delta": {
                            key: after_io.get(key, 0) - before_io.get(key, 0)
                            for key in after_io
                        },
                    }
                ),
            }
        )


def _payload_profile(payload_bytes: int) -> dict[str, int]:
    if payload_bytes == 0:
        return {
            "binding_count": 1,
            "fingerprint_padding_chars": 0,
        }
    if payload_bytes <= 4_096:
        return {
            "binding_count": 8,
            "fingerprint_padding_chars": 0,
        }
    # A bounded number of long-but-valid synthetic fingerprints keeps the
    # resulting receipt near the requested stress target while remaining below
    # the WorkReceipt binding limit.  The 500-character allowance covers the
    # non-padding JSON for one binding; the stored size is still measured and
    # the real 1,000,000-byte contract remains the final gate.
    fingerprint_padding_chars = min(
        4_096,
        max(0, payload_bytes // STRESS_BINDING_COUNT - 500),
    )
    return {
        "binding_count": STRESS_BINDING_COUNT,
        "fingerprint_padding_chars": fingerprint_padding_chars,
    }


def _fingerprint(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _synthetic_fingerprint(label: str, padding_chars: int) -> tuple[str, str]:
    digest = _fingerprint(label)
    if padding_chars <= 0:
        return digest, "sha256"
    marker = f"{digest}:a04-synthetic-padding:"
    value = (marker * ((padding_chars // len(marker)) + 1))[:padding_chars]
    return value, "synthetic-a04-padding-v1"


def _receipt_inputs(receipt_index: int, payload_bytes: int) -> tuple[dict[str, object], ...]:
    profile = _payload_profile(payload_bytes)
    bindings: list[dict[str, object]] = []
    for slot in range(profile["binding_count"]):
        name = f"a04-input:{receipt_index:08d}:{slot:04d}"
        fingerprint, algorithm = _synthetic_fingerprint(
            f"a04-input:{receipt_index}:{slot}",
            profile["fingerprint_padding_chars"],
        )
        bindings.append(
            {
                "kind": "a04_synthetic_input",
                "entity_id": f"a04-source:{receipt_index:08d}:{slot:04d}",
                "binding_name": name,
                "revision_id": f"a04-revision:{receipt_index:08d}:{slot:04d}",
                "fingerprint": {
                    "algorithm": algorithm,
                    "value": fingerprint,
                },
            }
        )
    return tuple(bindings)


def _estimated_receipt_bytes(payload_bytes: int) -> int:
    profile = _payload_profile(payload_bytes)
    return 8_192 + profile["binding_count"] * (
        450 + profile["fingerprint_padding_chars"]
    )


def _validate_point_admission(
    counts: Sequence[int],
    payload_bytes: int,
) -> str | None:
    if payload_bytes > 4_096 and any(count > MAX_LARGE_PAYLOAD_COUNT for count in counts):
        return (
            "payload profiles above 4096 bytes are limited to "
            f"{MAX_LARGE_PAYLOAD_COUNT} receipts"
        )
    receipt_upper = _estimated_receipt_bytes(payload_bytes)
    reader_upper = max(counts) * (2 * receipt_upper + 4_096)
    projection_upper = max(counts) * (receipt_upper + 1_024)
    if reader_upper > MAX_READER_BYTES:
        return "requested fixture exceeds the bounded reader hydration budget"
    if projection_upper > MAX_PROJECTION_BYTES:
        return "requested fixture exceeds the bounded projection budget"
    if max(counts) * receipt_upper > MAX_ESTIMATED_TOTAL_RECEIPT_BYTES:
        return "requested fixture exceeds the bounded synthetic receipt budget"
    return None


def _receipt_kwargs(receipt_index: int, payload_bytes: int) -> dict[str, object]:
    receipt_key = f"semantic-a04-receipt:{receipt_index:08d}"
    now_ns = BASE_TIME_NS + receipt_index
    return {
        "receipt_key": receipt_key,
        "stage_id": STAGE_ID,
        "stage_version": STAGE_VERSION,
        "processing_signature": PROCESSING_SIGNATURE,
        "status": "succeeded",
        "execution_mode": "executed",
        "reproducibility_class": "environment_bound",
        "entity_kind": "a04_synthetic_receipt",
        "entity_id": f"a04-entity:{receipt_index:08d}",
        "inputs": _receipt_inputs(receipt_index, payload_bytes),
        "outputs": (
            {
                "kind": "a04_synthetic_output",
                "entity_id": f"a04-output:{receipt_index:08d}",
                "fingerprint": {
                    "algorithm": "sha256",
                    "value": _fingerprint(f"a04-output:{receipt_index}"),
                },
            },
        ),
        "effective_config": {},
        "provider": {"provider": "synthetic-a04", "provider_version": "1"},
        "item_revision_id": None,
        "chunk_revision_id": None,
        "generation_id": None,
        "model_signature": None,
        "payload_id": None,
        "job_id": None,
        "attempt": 1,
        "started_ns": now_ns,
        "finished_ns": now_ns,
        "error": None,
        "causation_receipt_id": None,
        "event_kind": "semantic_a04_receipt_recorded",
        "aggregate_kind": "a04_synthetic_receipt",
        "aggregate_id": f"a04-aggregate:{receipt_index:08d}",
        "committed_ns": now_ns,
    }


class _ReceiptWriterApi:
    def __init__(self, function: Any, *, schema_version: int) -> None:
        if not callable(function):
            raise BenchmarkConfigurationError("Semantic receipt writer is not callable")
        self.function = function
        self.schema_version = schema_version

    def call(self, connection: Any, kwargs: Mapping[str, object]) -> object:
        # The fixture intentionally calls the real stable API with its exact
        # receipt kwargs.  Wire selection belongs to the owner/schema, not to
        # benchmark-side guessed parameters.
        return self.function(connection, **dict(kwargs))

    def metadata(self) -> dict[str, object]:
        return {
            "call_contract": "semantic_lineage_repository._record_work_receipt_v1_kwargs",
            "schema_version": self.schema_version,
            "wire_selection": "owner_schema_observed",
        }


def _schema_version(database: Path, semantic_schema: Any) -> int:
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise BenchmarkExecutionError("owner schema version is unavailable")
    version = int(row[0])
    if version not in KNOWN_SEMANTIC_SCHEMAS:
        raise BenchmarkExecutionError(
            f"unsupported owner schema {version}; expected one of {sorted(KNOWN_SEMANTIC_SCHEMAS)}"
        )
    return version


def _clear_callbacks(connection: Any) -> None:
    if connection is None:
        return
    for operation in (
        lambda: connection.set_trace_callback(None),
        lambda: connection.set_progress_handler(None, 0),
    ):
        try:
            operation()
        except sqlite3.ProgrammingError as error:
            if "closed" not in str(error).casefold():
                raise


def _write_receipts(
    database: Path,
    *,
    count: int,
    payload_bytes: int,
    semantic_schema: Any,
    receipt_api: _ReceiptWriterApi,
    sql_diagnostics: bool,
) -> dict[str, object]:
    trace = _SQLTrace() if sql_diagnostics else None
    returned_ids: list[int] = []
    connection: Any | None = None
    with _phase_metrics() as metrics:
        try:
            with semantic_schema.semantic_database(database) as opened:
                connection = opened
                if trace is not None:
                    connection.set_trace_callback(trace.observe)
                    connection.set_progress_handler(trace.progress, SQL_VM_INTERVAL)
                for receipt_index in range(count):
                    result = receipt_api.call(
                        connection,
                        _receipt_kwargs(receipt_index, payload_bytes),
                    )
                    if isinstance(result, bool) or not isinstance(result, int) or result < 1:
                        raise BenchmarkExecutionError(
                            f"receipt API returned an invalid id at index {receipt_index}: {result!r}"
                        )
                    returned_ids.append(result)
        finally:
            if trace is not None:
                _clear_callbacks(connection)
    metrics.update(
        {
            "phase": "writer_receipt_commit",
            "receipt_count_requested": count,
            "receipt_api_calls": len(returned_ids),
            "receipt_ids_first": returned_ids[0] if returned_ids else None,
            "receipt_ids_last": returned_ids[-1] if returned_ids else None,
            "receipt_ids_contiguous": returned_ids
            == list(range(1, len(returned_ids) + 1)),
            "sql": _sql_report(trace),
        }
    )
    return metrics


def _clone_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _clone_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_json(item) for item in value]
    return value


def _normalize_work_receipt(value: object) -> object:
    """Normalize only typed Semantic schema metadata in a validated receipt.

    The raw receipt remains untouched.  This comparison-only copy maps the
    known forward owner transition 9 -> 8 while preserving value types and
    every other field, including arbitrary configuration keys.
    """

    normalized = _clone_json(value)
    if (
        not isinstance(normalized, dict)
        or normalized.get("kind") != "work_receipt"
        or normalized.get("owner") != "semantic"
    ):
        return normalized
    runtime = normalized.get("runtime")
    if isinstance(runtime, dict):
        semantic_schema = runtime.get("semantic_schema")
        if semantic_schema == "9":
            runtime["semantic_schema"] = "8"
        elif semantic_schema == 9 and not isinstance(semantic_schema, bool):
            runtime["semantic_schema"] = 8
    for section_name in ("inputs", "outputs"):
        section = normalized.get(section_name)
        if not isinstance(section, list):
            continue
        for binding in section:
            if not isinstance(binding, dict):
                continue
            materialization = binding.get("materialization")
            if (
                isinstance(materialization, dict)
                and materialization.get("kind") == "materialization_ref"
                and materialization.get("owner") == "semantic"
            ):
                owner_schema_version = materialization.get("owner_schema_version")
                if (
                    isinstance(owner_schema_version, int)
                    and not isinstance(owner_schema_version, bool)
                    and owner_schema_version in {8, 9}
                ):
                    materialization["owner_schema_version"] = 8
    return normalized


def _hash_update(digest: Any, value: object) -> None:
    encoded = value if isinstance(value, bytes) else str(value).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _storage_snapshot(database: Path, semantic_schema: Any) -> dict[str, object]:
    receipt_raw_digest = hashlib.sha256()
    receipt_logical_digest = hashlib.sha256()
    outbox_raw_digest = hashlib.sha256()
    outbox_logical_digest = hashlib.sha256()
    receipt_rows = receipt_bytes = outbox_rows = outbox_bytes = 0
    max_receipt_bytes = max_outbox_payload_bytes = 0
    wire_schemas: Counter[str] = Counter()
    orphan_rows = 0
    pragmas: dict[str, object] = {}
    table_counts: dict[str, int] = {}
    dbstat: list[dict[str, object]] | None = None
    dbstat_by_type: dict[str, dict[str, int]] | None = None
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        for name in ("page_count", "page_size", "freelist_count", "journal_mode", "auto_vacuum"):
            try:
                pragmas[name] = connection.execute(f"PRAGMA {name}").fetchone()[0]
            except sqlite3.DatabaseError as error:
                pragmas[name] = f"unavailable:{type(error).__name__}"
        for table in ("semantic_work_receipts", "semantic_derivation_outbox"):
            table_counts[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        for row in connection.execute(
            "SELECT receipt_id,receipt_json FROM semantic_work_receipts ORDER BY receipt_id"
        ):
            receipt_rows += 1
            raw = str(row[1]).encode("utf-8")
            receipt_bytes += len(raw)
            max_receipt_bytes = max(max_receipt_bytes, len(raw))
            if len(raw) > MAX_RECEIPT_BYTES:
                raise BenchmarkExecutionError("stored WorkReceipt exceeds the 1,000,000-byte bound")
            _hash_update(receipt_raw_digest, str(row[0]).encode("ascii") + b"\0" + raw)
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError) as error:
                raise BenchmarkExecutionError("stored receipt JSON is malformed") from error
            _hash_update(
                receipt_logical_digest,
                str(row[0]).encode("ascii")
                + b"\0"
                + _canonical_json(_normalize_work_receipt(parsed)).encode("utf-8"),
            )
        for row in connection.execute(
            """SELECT event.event_id,event.receipt_id,event.event_kind,
                event.aggregate_kind,event.aggregate_id,event.payload_json,
                event.committed_ns,receipt.receipt_key,receipt.receipt_json
            FROM semantic_derivation_outbox AS event
            LEFT JOIN semantic_work_receipts AS receipt
              ON receipt.receipt_id=event.receipt_id
            ORDER BY event.event_id"""
        ):
            outbox_rows += 1
            raw_payload = str(row[5]).encode("utf-8")
            outbox_bytes += len(raw_payload)
            max_outbox_payload_bytes = max(max_outbox_payload_bytes, len(raw_payload))
            _hash_update(
                outbox_raw_digest,
                str(row[0]).encode("ascii") + b"\0" + raw_payload,
            )
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError) as error:
                raise BenchmarkExecutionError("stored outbox JSON is malformed") from error
            if isinstance(payload, Mapping) and isinstance(payload.get("schema"), str):
                wire_schemas[str(payload["schema"])] += 1
            if row[8] is None:
                orphan_rows += 1
                continue
            try:
                receipt = json.loads(str(row[8]))
            except (TypeError, ValueError) as error:
                raise BenchmarkExecutionError("joined receipt JSON is malformed") from error
            logical_event = {
                "event_id": int(row[0]),
                "receipt_id": int(row[1]),
                "event_kind": str(row[2]),
                "aggregate_kind": str(row[3]),
                "aggregate_id": str(row[4]),
                "committed_ns": int(row[6]),
                "receipt_key": str(row[7]),
                "receipt": _normalize_work_receipt(receipt),
            }
            _hash_update(outbox_logical_digest, _canonical_json(logical_event).encode("utf-8"))
        object_types: dict[str, str] = {}
        try:
            object_rows = connection.execute(
                "SELECT name,type FROM sqlite_master WHERE name IS NOT NULL"
            ).fetchall()
        except sqlite3.DatabaseError:
            object_types = {}
        else:
            object_types = {
                str(row[0]): str(row[1])
                for row in object_rows
                if row[0] is not None and row[1] is not None
            }
        try:
            dbstat_rows = connection.execute(
                "SELECT name,SUM(pgsize),COUNT(*) FROM dbstat GROUP BY name ORDER BY name"
            ).fetchall()
        except sqlite3.DatabaseError:
            dbstat = None
            dbstat_by_type = None
        else:
            dbstat = []
            by_type: dict[str, dict[str, int]] = {}
            for row in dbstat_rows:
                name = str(row[0])
                object_type = object_types.get(name, "unknown")
                bytes_value = int(row[1])
                pages_value = int(row[2])
                dbstat.append(
                    {
                        "name": name,
                        "type": object_type,
                        "bytes": bytes_value,
                        "pages": pages_value,
                    }
                )
                totals = by_type.setdefault(object_type, {"bytes": 0, "pages": 0})
                totals["bytes"] += bytes_value
                totals["pages"] += pages_value
            dbstat_by_type = dict(sorted(by_type.items()))
    return {
        "files": _owner_files(database),
        "fences": _owner_fences(database),
        "pragmas": pragmas,
        "tables": table_counts,
        "receipts": {
            "rows": receipt_rows,
            "json_bytes": receipt_bytes,
            "max_json_bytes": max_receipt_bytes,
            "raw_sha256": receipt_raw_digest.hexdigest(),
            "normalized_sha256": receipt_logical_digest.hexdigest(),
        },
        "outbox": {
            "rows": outbox_rows,
            "payload_bytes": outbox_bytes,
            "max_payload_bytes": max_outbox_payload_bytes,
            "raw_sha256": outbox_raw_digest.hexdigest(),
            "logical_sha256": outbox_logical_digest.hexdigest(),
            "wire_schema_counts": dict(sorted(wire_schemas.items())),
            "orphan_rows": orphan_rows,
        },
        "dbstat": dbstat,
        "dbstat_by_type": dbstat_by_type,
    }


_READER_TRACE: ContextVar[_SQLTrace | None] = ContextVar(
    "semantic_a04_reader_trace",
    default=None,
)


def _traced_reader_database(original: Any) -> Any:
    @contextlib.contextmanager
    def wrapper(path: Path, *args: object, **kwargs: object):
        trace = _READER_TRACE.get()
        connection: Any | None = None
        try:
            with original(path, *args, **kwargs) as opened:
                connection = opened
                if trace is not None:
                    connection.set_trace_callback(trace.observe)
                    connection.set_progress_handler(trace.progress, SQL_VM_INTERVAL)
                yield connection
        finally:
            if trace is not None:
                _clear_callbacks(connection)

    return wrapper


def _reader_call(function: Any, database: Path, cursor: int) -> Sequence[Any]:
    # This is the stable public reader contract in both benchmark generations;
    # do not infer optional parameters from a future signature.
    page = function(database, after_event_id=cursor, limit=PAGE_LIMIT)
    if isinstance(page, (str, bytes)) or not isinstance(page, Sequence):
        raise BenchmarkExecutionError("outbox reader did not return a bounded sequence")
    if len(page) > PAGE_LIMIT:
        raise BenchmarkExecutionError("outbox reader exceeded the requested page limit")
    return page


def _event_dto(event: Any) -> dict[str, object]:
    return {
        "event_id": int(event.event_id),
        "receipt_id": int(event.receipt_id),
        "event_kind": str(event.event_kind),
        "aggregate_kind": str(event.aggregate_kind),
        "aggregate_id": str(event.aggregate_id),
        "payload": event.payload,
        "receipt": event.receipt,
        "committed_ns": int(event.committed_ns),
    }


def _normalize_event_dto(value: Mapping[str, object]) -> object:
    normalized = _clone_json(value)
    if not isinstance(normalized, dict):
        return normalized
    if "receipt" in normalized:
        normalized["receipt"] = _normalize_work_receipt(normalized["receipt"])
    payload = normalized.get("payload")
    if isinstance(payload, dict) and "receipt" in payload:
        payload["receipt"] = _normalize_work_receipt(payload["receipt"])
    return normalized


def _event_digests(events: Sequence[Any]) -> dict[str, str]:
    dto_raw = hashlib.sha256()
    dto_comparison = hashlib.sha256()
    payload_raw = hashlib.sha256()
    payload_comparison = hashlib.sha256()
    for event in events:
        dto = _event_dto(event)
        raw_dto_json = _canonical_json(dto).encode("utf-8")
        comparison_dto_json = _canonical_json(_normalize_event_dto(dto)).encode("utf-8")
        raw_payload_json = _canonical_json(dto["payload"]).encode("utf-8")
        comparison_payload = _clone_json(dto["payload"])
        if isinstance(comparison_payload, dict) and "receipt" in comparison_payload:
            comparison_payload["receipt"] = _normalize_work_receipt(
                comparison_payload["receipt"]
            )
        comparison_payload_json = _canonical_json(comparison_payload).encode("utf-8")
        _hash_update(dto_raw, raw_dto_json)
        _hash_update(dto_comparison, comparison_dto_json)
        _hash_update(payload_raw, raw_payload_json)
        _hash_update(payload_comparison, comparison_payload_json)
    return {
        "dto_raw_sha256": dto_raw.hexdigest(),
        "dto_comparison_sha256": dto_comparison.hexdigest(),
        "payload_raw_sha256": payload_raw.hexdigest(),
        "payload_comparison_sha256": payload_comparison.hexdigest(),
    }


def _comparison_projection_events(
    projection_module: Any,
    projection_events: Sequence[Any],
) -> tuple[Any, ...]:
    event_type = projection_module.DerivationProjectionEvent
    return tuple(
        event_type(
            event.owner,
            int(event.cursor),
            str(event.event_id),
            _normalize_work_receipt(event.receipt),
        )
        for event in projection_events
    )


def _read_and_project(
    database: Path,
    *,
    count: int,
    semantic_lineage: Any,
    projection_module: Any,
    sql_diagnostics: bool,
) -> dict[str, object]:
    trace = _SQLTrace() if sql_diagnostics else None
    token = _READER_TRACE.set(trace)
    original_factory = semantic_lineage.semantic_database
    patched_factory = False
    if trace is not None:
        semantic_lineage.semantic_database = _traced_reader_database(original_factory)
        patched_factory = True
    events: list[Any] = []
    after_event_id = 0
    pages = 0
    hydration_metrics: dict[str, object] = {}
    try:
        with _phase_metrics() as hydration_metrics:
            while True:
                page = _reader_call(
                    semantic_lineage.read_semantic_derivation_outbox,
                    database,
                    after_event_id,
                )
                if not page:
                    break
                pages += 1
                if pages > count + 1:
                    raise BenchmarkExecutionError("outbox reader exceeded bounded page count")
                for event in page:
                    event_id = int(event.event_id)
                    if event_id <= after_event_id:
                        raise BenchmarkExecutionError("outbox cursor did not advance")
                    after_event_id = event_id
                    events.append(event)
            if len(events) != count:
                raise BenchmarkExecutionError(
                    f"reader returned {len(events)} events for {count} receipts"
                )
    finally:
        if patched_factory:
            semantic_lineage.semantic_database = original_factory
        _READER_TRACE.reset(token)

    hydrated_bytes = sum(
        len(_canonical_json(event.receipt).encode("utf-8"))
        + len(_canonical_json(event.payload).encode("utf-8"))
        for event in events
    )
    if hydrated_bytes > MAX_READER_BYTES:
        raise BenchmarkExecutionError("reader hydrated-byte budget exceeded")
    wire_schemas: Counter[str] = Counter()
    for event in events:
        payload = getattr(event, "payload", {})
        if isinstance(payload, Mapping) and isinstance(payload.get("schema"), str):
            wire_schemas[str(payload["schema"])] += 1
    event_digests = _event_digests(events)
    projection_input_bytes = sum(
        len(
            _canonical_json(
                {
                    "owner": "semantic",
                    "cursor": int(event.event_id),
                    "event_id": f"semantic:{int(event.event_id)}",
                    "receipt": event.receipt,
                }
            ).encode("utf-8")
        )
        for event in events
    )
    projection_events: tuple[Any, ...] = ()
    first_projection: Any | None = None
    second_projection: Any | None = None
    with _phase_metrics() as projection_metrics:
        projection_events = tuple(
            projection_module.projection_event_from_semantic_outbox(event)
            for event in events
        )
        if projection_input_bytes <= MAX_PROJECTION_BYTES:
            first_projection = projection_module.rebuild_derivation_projection(
                projection_events
            )
            second_projection = projection_module.rebuild_derivation_projection(
                projection_events
            )
    projection_metrics["phase"] = "projection_replay"

    projection_status = "complete" if first_projection is not None else "abstained_byte_budget"
    raw_replay_equal: bool | None = None
    comparison_replay_equal: bool | None = None
    raw_projection_fingerprint: str | None = None
    comparison_projection_fingerprint: str | None = None
    projection_events_applied: int | None = None
    projection_nodes: int | None = None
    projection_edges: int | None = None
    if first_projection is not None and second_projection is not None:
        first_raw_dict = first_projection.to_dict()
        second_raw_dict = second_projection.to_dict()
        raw_replay_equal = first_raw_dict == second_raw_dict
        raw_projection_fingerprint = hashlib.sha256(
            _canonical_json(first_raw_dict).encode("utf-8")
        ).hexdigest()
        comparison_events = _comparison_projection_events(
            projection_module,
            projection_events,
        )
        first_comparison = projection_module.rebuild_derivation_projection(comparison_events)
        second_comparison = projection_module.rebuild_derivation_projection(comparison_events)
        first_comparison_dict = first_comparison.to_dict()
        second_comparison_dict = second_comparison.to_dict()
        comparison_replay_equal = first_comparison_dict == second_comparison_dict
        comparison_projection_fingerprint = hashlib.sha256(
            _canonical_json(first_comparison_dict).encode("utf-8")
        ).hexdigest()
        projection_events_applied = int(first_projection.events_applied)
        projection_nodes = len(first_projection.nodes)
        projection_edges = len(first_projection.edges)

    projection_payload: dict[str, object] = {
        "status": projection_status,
        "input_bytes": projection_input_bytes,
        "max_bytes": MAX_PROJECTION_BYTES,
        "events_applied": projection_events_applied,
        "nodes": projection_nodes,
        "edges": projection_edges,
        "replay_equal": raw_replay_equal,
        "raw_replay_equal": raw_replay_equal,
        "comparison_replay_equal": comparison_replay_equal,
        "raw_fingerprint_sha256": raw_projection_fingerprint,
        "comparison_fingerprint_sha256": comparison_projection_fingerprint,
    }
    hydration_metrics.update(
        {
            "phase": "reader_hydration",
            "page_count": pages,
            "events_read": len(events),
            "cursor_start": 0,
            "cursor_final": after_event_id,
            "cursor_exhausted": True,
            "hydrated_receipts": len(events),
            "hydrated_bytes_observed": hydrated_bytes,
            "wire_schema_counts": dict(sorted(wire_schemas.items())),
            "hydration_fingerprint_sha256": event_digests["dto_comparison_sha256"],
            "hydration_dto_raw_sha256": event_digests["dto_raw_sha256"],
            "hydration_dto_comparison_sha256": event_digests["dto_comparison_sha256"],
            "hydration_payload_raw_sha256": event_digests["payload_raw_sha256"],
            "hydration_payload_comparison_sha256": event_digests[
                "payload_comparison_sha256"
            ],
            "projection": projection_payload,
            "projection_replay_equal": raw_replay_equal,
            "projection_fingerprint_sha256": comparison_projection_fingerprint,
            "sql": _sql_report(trace),
            "projection_phase": projection_metrics,
        }
    )
    return hydration_metrics


def _run_point(
    *,
    count: int,
    payload_bytes: int,
    run_root: Path,
    semantic_schema: Any,
    semantic_lineage: Any,
    projection_module: Any,
    sql_diagnostics: bool,
) -> dict[str, object]:
    database = run_root / f"semantic-{count}-{payload_bytes}.sqlite3"
    from neocortex.semantic.semantic_state import initialize_semantic_state

    point_started = time.perf_counter()
    initialize_semantic_state(database)
    schema_version = _schema_version(database, semantic_schema)
    receipt_api = _ReceiptWriterApi(
        semantic_lineage._record_work_receipt,
        schema_version=schema_version,
    )
    storage_before = _storage_snapshot(database, semantic_schema)
    writer = _write_receipts(
        database,
        count=count,
        payload_bytes=payload_bytes,
        semantic_schema=semantic_schema,
        receipt_api=receipt_api,
        sql_diagnostics=sql_diagnostics,
    )
    storage_after_writer = _storage_snapshot(database, semantic_schema)
    reader = _read_and_project(
        database,
        count=count,
        semantic_lineage=semantic_lineage,
        projection_module=projection_module,
        sql_diagnostics=sql_diagnostics,
    )
    storage_after_reader = _storage_snapshot(database, semantic_schema)
    projection = reader.get("projection")
    failures: list[str] = []
    if storage_after_writer["receipts"]["rows"] != count:
        failures.append("receipt_row_count")
    if storage_after_writer["outbox"]["rows"] != count:
        failures.append("outbox_row_count")
    if storage_after_writer["outbox"]["orphan_rows"] != 0:
        failures.append("outbox_orphan_rows")
    preservation_checks: dict[str, bool] = {}
    preservation_checks["files"] = storage_after_reader["files"] == storage_after_writer["files"]
    preservation_checks["fences"] = (
        storage_after_reader["fences"] == storage_after_writer["fences"]
    )
    preservation_checks["pragmas"] = (
        storage_after_reader["pragmas"] == storage_after_writer["pragmas"]
    )
    preservation_checks["tables"] = storage_after_reader["tables"] == storage_after_writer["tables"]
    for section in ("receipts", "outbox"):
        left_section = storage_after_reader[section]
        right_section = storage_after_writer[section]
        preservation_checks[f"{section}_raw_sha256"] = (
            left_section["raw_sha256"] == right_section["raw_sha256"]
        )
    failures.extend(
        f"reader_{name}_delta" for name, preserved in preservation_checks.items() if not preserved
    )
    if reader.get("events_read") != count or reader.get("cursor_exhausted") is not True:
        failures.append("reader_coverage")
    if not isinstance(projection, Mapping) or projection.get("status") != "complete":
        failures.append("projection_not_complete")
    if reader.get("projection_replay_equal") is not True:
        failures.append("projection_replay_equivalence")
    if not isinstance(projection, Mapping) or projection.get("comparison_replay_equal") is not True:
        failures.append("projection_comparison_equivalence")
    if reader.get("wire_schema_counts") != {WIRE_V1: count}:
        failures.append("public_payload_schema_mismatch")
    observed_wire = storage_after_writer["outbox"]["wire_schema_counts"]
    expected_wire = EXPECTED_SCHEMA_TO_WIRE[schema_version]
    if observed_wire != {expected_wire: count}:
        failures.append("wire_schema_mismatch")
    return {
        "count": count,
        "payload_bytes_requested": payload_bytes,
        "payload_profile": _payload_profile(payload_bytes),
        "schema_version": schema_version,
        "expected_wire_schema": expected_wire,
        "receipt_contract": RECEIPT_CONTRACT,
        "receipt_api": receipt_api.metadata(),
        "status": "complete" if not failures else "failed",
        "failure_reasons": failures,
        "reader_storage_preserved": all(preservation_checks.values()),
        "reader_storage_preservation_checks": preservation_checks,
        "writer": writer,
        "reader": reader,
        "storage_before": storage_before,
        "storage_after_writer": storage_after_writer,
        "storage_after_reader": storage_after_reader,
        "point_elapsed_seconds": time.perf_counter() - point_started,
        "byte_budgets": {
            "max_receipt_json_bytes": MAX_RECEIPT_BYTES,
            "max_reader_hydrated_bytes": MAX_READER_BYTES,
            "max_reader_page_bytes": MAX_PAGE_BYTES,
            "max_projection_bytes": MAX_PROJECTION_BYTES,
        },
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--counts",
        nargs="+",
        type=int,
        default=None,
        help="receipt counts (1..5000); normal points are 100, 1000 and 5000",
    )
    parser.add_argument(
        "--payload-bytes",
        type=int,
        default=DEFAULT_PAYLOAD_BYTES,
        help="synthetic payload target in bytes (0..900000; 4096 and 900000 are supported profiles)",
    )
    parser.add_argument(
        "--repository-root",
        "--repo-root",
        dest="repository_root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="absolute NeoCortex checkout; it is never used as a fixture destination",
    )
    parser.add_argument(
        "--temp-root",
        "--work-root",
        dest="temp_root",
        type=Path,
        help="absolute private parent for temporary owners (never the checkout or product state)",
    )
    parser.add_argument(
        "--output",
        "--report",
        dest="output",
        type=Path,
        help="new absolute JSON report path or '-' for stdout; existing files are refused",
    )
    parser.add_argument(
        "--sql-diagnostics",
        action="store_true",
        help="enable bounded SQL verb/progress diagnostics; only allowed with counts <= 100",
    )
    args = parser.parse_args(argv)
    if args.counts is None:
        args.counts = NORMAL_COUNTS
    if (
        not args.counts
        or any(isinstance(value, bool) or not 1 <= value <= MAX_RECEIPTS for value in args.counts)
        or len(set(args.counts)) != len(args.counts)
    ):
        parser.error(f"--counts must contain unique values between 1 and {MAX_RECEIPTS}")
    if not 0 <= args.payload_bytes <= MAX_PAYLOAD_TARGET_BYTES:
        parser.error(f"--payload-bytes must be between 0 and {MAX_PAYLOAD_TARGET_BYTES}")
    if args.sql_diagnostics and any(count > 100 for count in args.counts):
        parser.error("--sql-diagnostics is limited to the <=100-receipt canary")
    admission_error = _validate_point_admission(args.counts, args.payload_bytes)
    if admission_error is not None:
        parser.error(admission_error + "; use a smaller count or payload")
    return args


def _inherited_audit_boundary(*, repository_root: Path) -> Path | None:
    raw = os.environ.get("NEOCORTEX_AUDIT_LAB_ROOT")
    if raw is None:
        return None
    if not raw.strip():
        raise BenchmarkConfigurationError("NEOCORTEX_AUDIT_LAB_ROOT is empty")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise BenchmarkConfigurationError(
            "NEOCORTEX_AUDIT_LAB_ROOT must be an absolute directory"
        )
    boundary = candidate.resolve()
    repository = repository_root.expanduser().resolve()
    if _path_is_within(boundary, repository) or _path_is_within(repository, boundary):
        raise BenchmarkConfigurationError("audit boundary must not overlap the checkout")
    # A caller's boundary normally contains its already-private HOME.  It is
    # not itself a fixture destination, so the destination/installed-state
    # ancestor check would incorrectly reject precisely that isolation setup.
    # Actual temp/output paths still pass their separate _safe_path checks.
    if not boundary.is_dir():
        raise BenchmarkConfigurationError(
            f"NEOCORTEX_AUDIT_LAB_ROOT is not an existing directory: {boundary}"
        )
    return boundary


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repository_root = args.repository_root.expanduser().resolve()
    original_environment = dict(os.environ)
    original_sys_path = list(sys.path)
    temp_parent: Path | None = None
    run_root: Path | None = None
    owns_temp_parent = False
    try:
        repository_root = _safe_path(
            repository_root,
            label="--repository-root",
            repository_root=None,
        )
        if not (repository_root / "neocortex").is_dir():
            raise BenchmarkConfigurationError(f"repository root is not NeoCortex: {repository_root}")
        inherited_boundary = _inherited_audit_boundary(repository_root=repository_root)
        if args.temp_root is None:
            temp_base = inherited_boundary or SYSTEM_TEMP_ROOT
            temp_parent = Path(
                tempfile.mkdtemp(
                    prefix="neocortex-a04-storage-",
                    dir=temp_base,
                )
            )
            owns_temp_parent = True
        else:
            temp_parent = _safe_path(
                args.temp_root,
                label="--temp-root",
                repository_root=repository_root,
            )
            if inherited_boundary is not None and not _path_is_within(
                temp_parent,
                inherited_boundary,
            ):
                raise BenchmarkConfigurationError(
                    "--temp-root must be inside inherited NEOCORTEX_AUDIT_LAB_ROOT"
                )
            temp_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_root = Path(tempfile.mkdtemp(prefix=f"run-{os.getpid()}-", dir=temp_parent))
        output_path = _output_path(
            args.output,
            repository_root=repository_root,
            temp_root=temp_parent,
        )
        if output_path is not None and _path_is_within(output_path, run_root):
            raise BenchmarkConfigurationError("--output must not be inside the run root")
        if str(args.output) == "-":
            output_path = None
        environment_report = _private_environment(
            run_root,
            inherited_boundary=inherited_boundary,
        )
        environment_report["run_root"] = str(run_root.resolve())
        environment_report["inherited_boundary"] = (
            None if inherited_boundary is None else str(inherited_boundary.resolve())
        )
        if str(repository_root) not in sys.path:
            sys.path.insert(0, str(repository_root))
        from neocortex.semantic import derivation_projection as projection_module
        from neocortex.semantic import semantic_lineage_repository as semantic_lineage
        from neocortex.semantic import semantic_schema

        results: list[dict[str, object]] = []
        for count in args.counts:
            try:
                results.append(
                    _run_point(
                        count=count,
                        payload_bytes=args.payload_bytes,
                        run_root=run_root,
                        semantic_schema=semantic_schema,
                        semantic_lineage=semantic_lineage,
                        projection_module=projection_module,
                        sql_diagnostics=args.sql_diagnostics,
                    )
                )
            except Exception as error:
                results.append(
                    {
                        "count": count,
                        "payload_bytes_requested": args.payload_bytes,
                        "status": "error",
                        "failure_reasons": [type(error).__name__],
                        "error": str(error)[:1_000],
                    }
                )
        failed = [result for result in results if result.get("status") != "complete"]
        report = {
            "schema": BENCHMARK_SCHEMA,
            "status": "complete" if not failed else "failed",
            "kind": "synthetic_receipt_outbox_storage",
            "repository_root": str(repository_root),
            "source_mode": "repo-native-synthetic-receipts",
            "environment_isolated": bool(environment_report.get("all_paths_under_run_root")),
            "environment": environment_report,
            "audit_lab_root_preserved": bool(environment_report.get("marker_preserved")),
            "model_real": False,
            "model_loaded": False,
            "sql_diagnostics": args.sql_diagnostics,
            "wire_contracts": {
                "schema8": WIRE_V1,
                "schema9": WIRE_V2,
                "receipt": RECEIPT_CONTRACT,
            },
            "comparison_normalization": {
                "transition": "semantic owner 9 -> 8",
                "receipt_runtime_key": "runtime.semantic_schema",
                "typed_locator": "MaterializationRef(owner=semantic).owner_schema_version",
                "other_fields_normalized": False,
                "raw_bytes_retained": True,
            },
            "schema_versions_supported": sorted(KNOWN_SEMANTIC_SCHEMAS),
            "counts": list(args.counts),
            "payload_bytes_requested": args.payload_bytes,
            "page_limit": PAGE_LIMIT,
            "fixed_time_base_ns": BASE_TIME_NS,
            "fixed_ids": True,
            "constraints": {
                "max_receipt_json_bytes": MAX_RECEIPT_BYTES,
                "max_reader_hydrated_bytes": MAX_READER_BYTES,
                "max_reader_page_bytes": MAX_PAGE_BYTES,
                "max_projection_bytes": MAX_PROJECTION_BYTES,
                "max_large_payload_count": MAX_LARGE_PAYLOAD_COUNT,
                "max_estimated_total_receipt_bytes": MAX_ESTIMATED_TOTAL_RECEIPT_BYTES,
                "no_vacuum_gc_retention": True,
            },
            "runtime": {
                "python": platform.python_version(),
                "python_implementation": platform.python_implementation(),
            },
            "results": results,
        }
        _write_report(output_path, report)
        return 1 if failed else 0
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
        sys.path[:] = original_sys_path
        if run_root is not None:
            shutil.rmtree(run_root, ignore_errors=True)
        if owns_temp_parent and temp_parent is not None:
            shutil.rmtree(temp_parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "BENCHMARK_SCHEMA",
    "BenchmarkConfigurationError",
    "BenchmarkExecutionError",
    "main",
)
