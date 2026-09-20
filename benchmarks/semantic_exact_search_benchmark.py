#!/usr/bin/env python3
"""Bounded, read-only benchmark for Semantic's exact vector-search path.

The benchmark deliberately exercises ``semantic_search_repository.search_exact_page``
instead of a replacement implementation.  Synthetic databases are created only
under a private temporary run root and are deleted after the run.  It does not
open an audit or installed-state snapshot and never loads a real model.

The synthetic fixture is a search-valid core Semantic publication containing a
Jina-shaped 768-dimensional float16 vector and representative metadata.  It
does not manufacture lineage receipts, so this script does not claim to test
lineage resolution or the public model-loading facade.  Those concerns belong
to separate audit tracks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import math
import multiprocessing
import os
import platform
import re
import resource
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import zlib
from collections import Counter
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO_ROOT = REPOSITORY_ROOT
SYSTEM_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
MAX_TEMP_BYTES = 20 * 1024**3
MAX_RSS_BYTES = 4 * 1024**3
NORMAL_SCALES = (100_000, 500_000)
CANARY_SCALES = (100, 1_000)
DEFAULT_SCALES = NORMAL_SCALES
SQL_VM_INTERVAL = 1_000
WARM_REPEATS = 5
DEFAULT_LIMIT = 20
DEFAULT_BATCH_SIZE = 512
DEFAULT_MAX_VECTORS = 500_000
DEFAULT_SEED = 20260912
BUILD_BATCH = 1_000
MODEL_SIGNATURE = (
    "fastembed-0.8.0|explicit-l2-v1|reject-token-truncation-v1|"
    "jinaai/jina-embeddings-v2-base-es|float16"
)
MODEL_VECTOR_SPACE = "jina-embeddings-v2-base-es-v1"
MODEL_ID = "jinaai/jina-embeddings-v2-base-es"
MODEL_VERSION = "fastembed-registry-0.8.0"
MODEL_DIMENSIONS = 768
MODEL_PROVIDER = "fastembed-onnx-cpu"
FIXTURE_CHUNKING_SIGNATURE = "natural-window-jina-512-exact-token-guard-v2"
FIXTURE_SCHEMA = "neocortex.semantic-exact-search-benchmark/v1"
EXPECTED_SEMANTIC_SCHEMA_VERSION = 10
REFERENCE_PREIMAGE_SHA256 = (
    "1f71614d3fe8d7e83e58b5f09c65aa3de92dd707f746c6176f160900ae74be13"
)


def _load_repository(repo_root: Path) -> None:
    resolved = repo_root.resolve(strict=True)
    if str(resolved) not in sys.path:
        sys.path.insert(0, str(resolved))


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _safe_absolute_path(
    path: Path,
    *,
    label: str,
    must_exist: bool = False,
    repository_root: Path | None = None,
) -> Path:
    """Resolve a path without allowing benchmark data in product state."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be absolute: {path}")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(candidate)
    resolved = candidate.resolve(strict=False)
    if repository_root is not None:
        repository = repository_root.expanduser().resolve()
        if _path_is_within(resolved, repository) or _path_is_within(repository, resolved):
            raise ValueError(f"{label} must not overlap the checkout: {resolved}")
    home = Path.home().resolve()
    product_roots = (
        home / ".config" / "Neocortex",
        home / ".local" / "share" / "Neocortex",
        home / ".local" / "state" / "Neocortex",
        home / ".cache" / "Neocortex",
    )
    if any(_path_is_within(resolved, root) for root in product_roots):
        raise ValueError(f"{label} must not overlap installed NeoCortex state: {resolved}")
    return resolved


def _fixture_model(model_signature: str) -> Any:
    """Return the exact registered metadata without loading an encoder."""

    from neocortex.semantic.semantic_models import (
        EmbeddingModality,
        EmbeddingModelSpec,
        EmbeddingRole,
        VectorDType,
    )

    if not model_signature.strip():
        raise ValueError("model signature cannot be blank")
    return EmbeddingModelSpec(
        model_signature=model_signature,
        vector_space=MODEL_VECTOR_SPACE,
        modality=EmbeddingModality.TEXT,
        model_id=MODEL_ID,
        model_version=MODEL_VERSION,
        dimensions=MODEL_DIMENSIONS,
        provider=MODEL_PROVIDER,
        supported_roles=(EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
        vector_dtype=VectorDType.FLOAT16,
        provenance={
            "license": "Apache-2.0",
            "languages": "Spanish-English",
            "normalization": "explicit-l2-in-adapter",
            "calibration": "retrieval-only-not-classification-calibrated",
            "selection": "quality-profile-local-retrieval-smoke-v1",
        },
    )


def _file_bytes(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for label, candidate in (
        ("database", path),
        ("wal", Path(f"{path}-wal")),
        ("shm", Path(f"{path}-shm")),
        ("journal", Path(f"{path}-journal")),
    ):
        try:
            result[label] = candidate.stat().st_size
        except FileNotFoundError:
            result[label] = 0
    result["total"] = sum(result.values())
    return result


def _file_fence(path: Path) -> dict[str, object]:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return {"status": "absent"}
    if path.is_symlink():
        return {"status": "symlink"}
    if not path.is_file():
        return {"status": "non_regular"}
    return {
        "status": "present",
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "ctime_ns": int(stat_result.st_ctime_ns),
        "mode": int(stat_result.st_mode & 0o777),
    }


def _owner_fences(database: Path) -> dict[str, dict[str, object]]:
    return {
        "database": _file_fence(database),
        "wal": _file_fence(Path(f"{database}-wal")),
        "shm": _file_fence(Path(f"{database}-shm")),
        "journal": _file_fence(Path(f"{database}-journal")),
    }


def _read_proc_status(pid: int = 0) -> dict[str, int] | None:
    selected = os.getpid() if pid == 0 else pid
    try:
        text = Path(f"/proc/{selected}/status").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    values: dict[str, int] = {}
    for key in ("VmRSS", "VmHWM", "VmSize"):
        match = re.search(rf"^{re.escape(key)}:\s+(\d+)\s+kB$", text, re.MULTILINE)
        if match:
            values[key] = int(match.group(1)) * 1024
    match = re.search(r"^Threads:\s+(\d+)$", text, re.MULTILINE)
    if match:
        values["Threads"] = int(match.group(1))
    return values


def _read_proc_io(pid: int = 0) -> dict[str, int] | None:
    selected = os.getpid() if pid == 0 else pid
    try:
        text = Path(f"/proc/{selected}/io").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    result: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        try:
            result[key.strip()] = int(raw.strip())
        except ValueError:
            continue
    return result


class _ProcessSampler:
    """Low-overhead Linux self-RSS sampler; no third-party dependency."""

    def __init__(self, interval_seconds: float = 0.05) -> None:
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak_rss = 0
        self.peak_vms = 0
        self.peak_threads = 0

    def _sample(self) -> None:
        status = _read_proc_status()
        if status is None:
            return
        self.peak_rss = max(self.peak_rss, status.get("VmRSS", 0))
        self.peak_vms = max(self.peak_vms, status.get("VmSize", 0))
        self.peak_threads = max(self.peak_threads, status.get("Threads", 0))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def start(self) -> None:
        self._sample()
        self._thread = threading.Thread(target=self._run, name="semantic-benchmark-rss", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._sample()


def _rusage() -> dict[str, float]:
    self_usage = resource.getrusage(resource.RUSAGE_SELF)
    children_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "self_user_seconds": float(self_usage.ru_utime),
        "self_system_seconds": float(self_usage.ru_stime),
        "children_user_seconds": float(children_usage.ru_utime),
        "children_system_seconds": float(children_usage.ru_stime),
    }


def _runtime_metadata() -> dict[str, object]:
    """Capture effective benchmark knobs without changing the environment."""

    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "multiprocessing_start_method": multiprocessing.get_start_method(allow_none=True),
        "multiprocessing_start_methods": multiprocessing.get_all_start_methods(),
        "environment_threads": {
            name: os.environ.get(name)
            for name in (
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
            if os.environ.get(name) is not None
        },
    }


def _delta(before: Mapping[str, float], after: Mapping[str, float]) -> dict[str, float]:
    return {key: float(after.get(key, 0.0) - before.get(key, 0.0)) for key in after}


class _SQLTrace:
    def __init__(self) -> None:
        self.trace_callbacks = 0
        self.vm_callbacks = 0
        self.statement_kinds: Counter[str] = Counter()
        self.statement_hashes: Counter[str] = Counter()

    def trace(self, statement: str) -> None:
        normalized = re.sub(r"\s+", " ", statement).strip()
        self.trace_callbacks += 1
        kind = normalized.split(" ", 1)[0].upper() if normalized else "EMPTY"
        self.statement_kinds[kind] += 1
        digest = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:16]
        self.statement_hashes[digest] += 1

    def progress(self) -> int:
        self.vm_callbacks += 1
        return 0

    def payload(self) -> dict[str, object]:
        return {
            "trace_callbacks": self.trace_callbacks,
            "vm_progress_callbacks": self.vm_callbacks,
            "vm_steps_lower_bound": self.vm_callbacks * SQL_VM_INTERVAL,
            "vm_progress_interval": SQL_VM_INTERVAL,
            "statement_kinds": dict(sorted(self.statement_kinds.items())),
            "statement_hashes": dict(sorted(self.statement_hashes.items())),
        }


_TRACE_CONTEXT: ContextVar[_SQLTrace | None] = ContextVar("semantic_benchmark_trace", default=None)


def _traced_semantic_database(original):
    """Trace one imported connection factory without ending before ``__exit__``."""

    @contextlib.contextmanager
    def wrapper(path: Path, *args: object, **kwargs: object):
        trace = _TRACE_CONTEXT.get()
        connection: Any | None = None
        try:
            with original(path, *args, **kwargs) as opened:
                connection = opened
                if trace is not None:
                    connection.set_trace_callback(trace.trace)
                    connection.set_progress_handler(trace.progress, SQL_VM_INTERVAL)
                yield connection
        finally:
            if trace is not None and connection is not None:
                try:
                    connection.set_trace_callback(None)
                    connection.set_progress_handler(None, 0)
                except sqlite3.ProgrammingError as cleanup_error:
                    if "closed" not in str(cleanup_error).casefold():
                        raise

    return wrapper


def _representative_vector_blob(index: int, dimensions: int, seed: int) -> bytes:
    """Make a deterministic finite nonzero vector without a corpus-wide matrix."""

    base = bytearray(_BASE_VECTOR_BYTES[: dimensions * 2])
    # Three float16 components encode the row number in base 2048.  This keeps
    # every payload unique through 500k rows while copying only one small blob.
    mixed_index = index + (seed % (2048**3))
    for position in range(3):
        digit = (mixed_index // (2048**position)) % 2048
        value = (digit - 1023.5) / 1024.0
        struct.pack_into("<e", base, position * 2, value)
    return bytes(base)


def _query_vector(dimensions: int, seed: int) -> tuple[float, ...]:
    values = [0.02 + ((position * 37) % 97) / 10_000.0 for position in range(dimensions)]
    values[0] = 0.31 + ((seed % 17) - 8) / 10_000.0
    values[1] = -0.27 + ((seed % 13) - 6) / 10_000.0
    values[2] = 0.19 + ((seed % 11) - 5) / 10_000.0
    return tuple(values)


_BASE_VECTOR_BYTES = struct.pack(
    "<768e",
    *[0.02 + ((position * 37) % 97) / 10_000.0 for position in range(768)],
)


def _query_fingerprint(dimensions: int, seed: int) -> str:
    payload = _json({
        "dimensions": dimensions,
        "seed": seed,
        "vector": [value.hex() for value in _query_vector(dimensions, seed)],
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _topk_fingerprint(hits: Sequence[Any]) -> str:
    payload = [
        {
            "ref_id": int(hit.ref_id),
            "entity_id": str(hit.entity_id),
            "item_id": str(hit.item_id),
            "indexed_model_signature": str(hit.indexed_model_signature),
            "vector_space": str(hit.vector_space),
            "modality": str(hit.modality.value),
            "score_hex": float(hit.score).hex(),
            "generation_id": int(hit.generation_id),
        }
        for hit in hits
    ]
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fixture_rows(
    index: int,
    generation_id: int,
    model_signature: str,
    dimensions: int,
    seed: int,
):
    item_id = f"item:benchmark:{index:08d}"
    source_identity = f"benchmark-source-{index:08d}"
    text = (
        f"Transformador {index:08d}; mantenimiento preventivo, diagnóstico, "
        "aislamiento y protección en patio eléctrico."
    )
    encoded = text.encode("utf-8")
    from neocortex.foundation.hash_compat import sha256

    fingerprint = sha256.sha256_128_hexdigest(encoded)
    guard = sha256.sha256_64_hexdigest(encoded, seed=0x4E454F43)
    content_bytes = len(encoded)
    source_revision = _json(
        {
            "size_bytes": content_bytes,
            "mtime_ns": 1_700_000_000_000_000_000 + index,
            "birthtime_ns": -1,
            "processing_signature": "semantic-search-benchmark-source-v1",
        }
    )
    item_provenance = _json(
        {
            "adapter": "semantic-search-benchmark-v1",
            "source": "synthetic",
            "source_status": "complete",
        }
    )
    chunk_provenance = _json(
        {"adapter": "semantic-search-benchmark-v1", "page": 1, "synthetic": True}
    )
    path = f"/benchmark/synthetic/document-{index:08d}.pdf"
    item = (
        item_id,
        "pdf",
        source_identity,
        "benchmark-source-v1",
        path,
        fingerprint,
        content_bytes,
        guard,
        item_provenance,
        source_revision,
        "benchmark-refresh",
        1,
        1_700_000_100_000_000_000 + index,
    )
    item_revision = (
        item_id,
        "pdf",
        source_identity,
        "benchmark-source-v1",
        path,
        fingerprint,
        content_bytes,
        guard,
        item_provenance,
        source_revision,
        1_700_000_100_000_000_000 + index,
    )
    chunk_id = f"chunk-xxh3-128:{fingerprint}"
    compressed = zlib.compress(encoded, level=6)
    chunk = (
        chunk_id,
        item_id,
        0,
        "pdf_page",
        "1",
        0,
        len(text),
        compressed,
        len(text),
        fingerprint,
        content_bytes,
        guard,
        FIXTURE_CHUNKING_SIGNATURE,
        chunk_provenance,
        "benchmark-refresh",
        1,
        1_700_000_100_000_000_000 + index,
    )
    chunk_revision = (
        chunk_id,
        item_id,
        0,
        "pdf_page",
        "1",
        0,
        len(text),
        compressed,
        len(text),
        fingerprint,
        content_bytes,
        guard,
        FIXTURE_CHUNKING_SIGNATURE,
        chunk_provenance,
        1_700_000_100_000_000_000 + index,
    )
    payload = (
        model_signature,
        fingerprint,
        content_bytes,
        guard,
        dimensions,
        "float16",
            _representative_vector_blob(index, dimensions, seed),
        1.0,
        _json(
            {
                "backend": "fastembed",
                "model_id": "jinaai/jina-embeddings-v2-base-es",
                "role": "passage",
                "token_count": 48,
                "token_limit": 512,
                "token_truncated": False,
                "benchmark": "synthetic-search-only",
            }
        ),
        1_700_000_100_000_000_000 + index,
    )
    return item, item_revision, chunk, chunk_revision, payload, (item_id, chunk_id, fingerprint, guard, content_bytes)


def _build_synthetic_fixture(
    database: Path,
    count: int,
    *,
    model_spec: Any,
    model_signature: str,
    dimensions: int,
    seed: int,
) -> int:
    """Build one bounded valid published core without retaining rows in RAM."""

    from neocortex.semantic.semantic_schema import initialize_semantic_state, semantic_database
    from neocortex.semantic.semantic_state import register_embedding_model

    if model_spec.model_signature != model_signature:
        raise RuntimeError("fixture model signature is not coherent with row signatures")
    if model_spec.dimensions != dimensions:
        raise RuntimeError("fixture dimensions are not coherent with model metadata")
    initialize_semantic_state(database)
    register_embedding_model(database, model_spec)
    with semantic_database(database) as connection:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version != EXPECTED_SEMANTIC_SCHEMA_VERSION:
            raise RuntimeError(
                "unsupported Semantic schema version for this exact-search fixture: "
                f"{schema_version}; expected {EXPECTED_SEMANTIC_SCHEMA_VERSION}"
            )
        connection.execute("BEGIN IMMEDIATE")
        generation_cursor = connection.execute(
            """INSERT INTO embedding_generations(
                model_signature,processing_signature,status,provenance_json,
                cursor_json,started_ns,completed_ns,pending_count,leased_count,
                done_count,error_count,stale_count,base_generation_id,base_clone_complete)
            VALUES(?,?, 'ready', ?, ?, ?, ?, 0, 0, ?, 0, 0, NULL, 1)""",
            (
                model_signature,
                "semantic-search-benchmark-generation-v1",
                _json(
                    {
                        "pipeline": "semantic-search-benchmark-v1",
                        "sources": ["pdf"],
                        "chunking_signature": FIXTURE_CHUNKING_SIGNATURE,
                        "benchmark": "synthetic-search-only",
                    }
                ),
                _json({"protocol": "benchmark-v1", "enumeration_complete": True}),
                1_700_000_000_000_000_000,
                1_700_000_100_000_000_000,
                count,
            ),
        )
        generation_id = int(generation_cursor.lastrowid)
        connection.commit()

        for start in range(0, count, BUILD_BATCH):
            stop = min(count, start + BUILD_BATCH)
            item_rows: list[tuple[object, ...]] = []
            item_revision_rows: list[tuple[object, ...]] = []
            chunk_rows: list[tuple[object, ...]] = []
            chunk_revision_rows: list[tuple[object, ...]] = []
            payload_rows: list[tuple[object, ...]] = []
            member_rows: list[tuple[object, ...]] = []
            embedding_rows: list[tuple[object, ...]] = []
            for index in range(start, stop):
                item, item_revision, chunk, chunk_revision, payload, binding = _fixture_rows(
                    index, generation_id, model_signature, dimensions, seed
                )
                item_rows.append(item)
                item_revision_rows.append(item_revision)
                chunk_rows.append(chunk)
                chunk_revision_rows.append(chunk_revision)
                payload_rows.append(payload)
                item_id, chunk_id, fingerprint, guard, content_bytes = binding
                # The primary key is obtained in a second lookup-free step by
                # using the deterministic payload ordering below.  SQLite's
                # AUTOINCREMENT values are contiguous in this isolated builder.
                payload_id = start + (index - start) + 1
                item_revision_id = payload_id
                chunk_revision_id = payload_id
                provenance = str(payload[8])
                member_rows.append(
                    (
                        generation_id,
                        model_signature,
                        "text_chunk",
                        chunk_id,
                        item_id,
                        item_revision_id,
                        chunk_revision_id,
                        payload_id,
                        fingerprint,
                        content_bytes,
                        guard,
                        provenance,
                        1_700_000_100_000_000_000 + index,
                        None,
                    )
                )
                embedding_rows.append(
                    (
                        chunk_id,
                        model_signature,
                        payload_id,
                        generation_id,
                        fingerprint,
                        content_bytes,
                        guard,
                        provenance,
                        1_700_000_100_000_000_000 + index,
                    )
                )

            connection.executemany(
                """INSERT INTO semantic_items(
                    item_id,source_kind,source_identity,identity_version,path,
                    content_xxh3_128,content_bytes,content_xxh3_64_guard,
                    provenance_json,source_revision_json,refresh_token,active,updated_ns)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                item_rows,
            )
            connection.executemany(
                """INSERT INTO semantic_item_revisions(
                    item_id,source_kind,source_identity,identity_version,path,
                    content_xxh3_128,content_bytes,content_xxh3_64_guard,
                    provenance_json,source_revision_json,captured_ns)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                item_revision_rows,
            )
            connection.executemany(
                """INSERT INTO text_chunks(
                    chunk_id,item_id,ordinal,section_kind,section_id,start_char,end_char,
                    text_zlib,text_chars,content_xxh3_128,content_bytes,
                    content_xxh3_64_guard,chunking_signature,provenance_json,
                    refresh_token,active,updated_ns)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                chunk_rows,
            )
            connection.executemany(
                """INSERT INTO semantic_chunk_revisions(
                    chunk_id,item_id,ordinal,section_kind,section_id,start_char,end_char,
                    text_zlib,text_chars,content_xxh3_128,content_bytes,
                    content_xxh3_64_guard,chunking_signature,provenance_json,captured_ns)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                chunk_revision_rows,
            )
            connection.executemany(
                """INSERT INTO vector_payloads(
                    model_signature,content_xxh3_128,content_bytes,content_xxh3_64_guard,
                    dimensions,vector_dtype,vector_blob,original_norm,provenance_json,created_ns)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                payload_rows,
            )
            connection.executemany(
                """INSERT INTO embedding_generation_members(
                    generation_id,model_signature,entity_kind,entity_id,item_id,
                    item_revision_id,chunk_revision_id,payload_id,content_xxh3_128,
                    content_bytes,content_xxh3_64_guard,provenance_json,updated_ns,base_member_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                member_rows,
            )
            connection.executemany(
                """INSERT INTO text_embeddings(
                    chunk_id,model_signature,payload_id,generation_id,
                    content_xxh3_128,content_bytes,content_xxh3_64_guard,
                    provenance_json,updated_ns)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                embedding_rows,
            )
            connection.commit()
            if _file_bytes(database)["total"] > MAX_TEMP_BYTES:
                raise RuntimeError("synthetic fixture exceeded the 20 GiB temporary bound")
            if (_read_proc_status() or {}).get("VmRSS", 0) > MAX_RSS_BYTES:
                raise RuntimeError("synthetic fixture exceeded the 4 GiB RSS bound")
            if stop < count:
                connection.execute("BEGIN IMMEDIATE")

        connection.execute(
            "INSERT INTO published_embedding_heads(model_signature,generation_id,published_ns) VALUES(?,?,?)",
            (model_signature, generation_id, 1_700_000_100_000_000_000),
        )
        connection.commit()
    return generation_id


def _sqlite_physical(database: Path) -> dict[str, object]:
    from neocortex.semantic.semantic_schema import semantic_database

    result: dict[str, object] = {"files": _file_bytes(database)}
    with semantic_database(database, readonly=True) as connection:
        for pragma in (
            "page_count",
            "page_size",
            "freelist_count",
            "auto_vacuum",
            "journal_mode",
            "wal_autocheckpoint",
            "cache_size",
        ):
            try:
                result[pragma] = connection.execute(f"PRAGMA {pragma}").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                result[pragma] = f"unavailable:{type(exc).__name__}"
        try:
            rows = connection.execute(
                "SELECT name, SUM(pgsize), COUNT(*) FROM dbstat GROUP BY name ORDER BY SUM(pgsize) DESC"
            ).fetchall()
        except sqlite3.DatabaseError:
            result["dbstat"] = None
        else:
            result["dbstat"] = [
                {"name": str(row[0]), "bytes": int(row[1]), "pages": int(row[2])}
                for row in rows
            ]
    return result


def _explain(
    database: Path,
    model_signature: str,
    generation_id: int,
    *,
    max_vectors: int,
) -> list[str]:
    from neocortex.semantic import semantic_search_repository as repository
    from neocortex.semantic.semantic_models import EmbeddingModality
    from neocortex.semantic.semantic_schema import semantic_database

    sql = repository._search_sql(EmbeddingModality.TEXT, 1, text_scope="all")
    with semantic_database(database, readonly=True) as connection:
        rows = connection.execute(
            "EXPLAIN QUERY PLAN " + sql,
            (model_signature, generation_id, 0, max_vectors + 1),
        ).fetchall()
    return [str(row[-1]) for row in rows]


def _invoke_search(
    database: Path,
    model_signature: str,
    vector_space: str,
    generation_id: int,
    dimensions: int,
    seed: int,
    *,
    limit: int,
    max_vectors: int,
    batch_size: int,
) -> dict[str, object]:
    from neocortex.semantic import semantic_search_repository as repository
    from neocortex.semantic.semantic_models import EmbeddingModality, ExactSearchQuery

    trace = _SQLTrace()
    token = _TRACE_CONTEXT.set(trace)
    started = time.perf_counter_ns()
    try:
        page = repository.search_exact_page(
            database,
            ExactSearchQuery(
                query_model_signature=model_signature,
                vector_space=vector_space,
                dimensions=dimensions,
                vector=_query_vector(dimensions, seed),
                target_modality=EmbeddingModality.TEXT,
                indexed_model_signatures=(model_signature,),
            ),
            limit=limit,
            max_vectors=max_vectors,
            batch_size=batch_size,
        )
    finally:
        _TRACE_CONTEXT.reset(token)
    elapsed = time.perf_counter_ns() - started
    return {
        "elapsed_ns": elapsed,
        "elapsed_seconds": elapsed / 1_000_000_000,
        "scanned": int(page.scanned),
        "complete": bool(page.complete),
        "next_cursor": page.next_cursor,
        "hits": len(page.hits),
        "topk_fingerprint_sha256": _topk_fingerprint(page.hits),
        "topk_ref_ids": [int(hit.ref_id) for hit in page.hits],
        "trace": trace.payload(),
    }


def _round(
    database: Path,
    model_signature: str,
    vector_space: str,
    generation_id: int,
    dimensions: int,
    seed: int,
    *,
    readers: int,
    limit: int,
    max_vectors: int,
    batch_size: int,
) -> dict[str, object]:
    owner_fences_before = _owner_fences(database)
    before_usage = _rusage()
    before_io = _read_proc_io()
    before_status = _read_proc_status() or {}
    sampler = _ProcessSampler()
    sampler.start()
    started = time.perf_counter_ns()
    try:
        if readers == 1:
            observations = [
                _invoke_search(
                    database,
                    model_signature,
                    vector_space,
                    generation_id,
                    dimensions,
                    seed,
                    limit=limit,
                    max_vectors=max_vectors,
                    batch_size=batch_size,
                )
            ]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=readers) as pool:
                futures = [
                    pool.submit(
                        _invoke_search,
                        database,
                        model_signature,
                        vector_space,
                        generation_id,
                        dimensions,
                        seed,
                        limit=limit,
                        max_vectors=max_vectors,
                        batch_size=batch_size,
                    )
                    for _ in range(readers)
                ]
                observations = [future.result() for future in futures]
    finally:
        sampler.stop()
    elapsed_ns = time.perf_counter_ns() - started
    owner_fences_after = _owner_fences(database)
    after_usage = _rusage()
    after_io = _read_proc_io()
    after_status = _read_proc_status() or {}
    return {
        "readers": readers,
        "elapsed_ns": elapsed_ns,
        "elapsed_seconds": elapsed_ns / 1_000_000_000,
        "observations": observations,
        "cpu": _delta(before_usage, after_usage),
        "io_delta": (
            None
            if before_io is None or after_io is None
            else {key: after_io.get(key, 0) - before_io.get(key, 0) for key in after_io}
        ),
        "rss_start_bytes": before_status.get("VmRSS"),
        "rss_end_bytes": after_status.get("VmRSS"),
        "rss_peak_bytes": sampler.peak_rss,
        "rss_delta_peak_bytes": max(0, sampler.peak_rss - before_status.get("VmRSS", 0)),
        "vms_peak_bytes": sampler.peak_vms,
        "peak_threads": sampler.peak_threads,
        "owner_fences": {
            "before": owner_fences_before,
            "after": owner_fences_after,
            "unchanged": owner_fences_before == owner_fences_after,
        },
    }


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _measure(
    database: Path,
    *,
    model_signature: str,
    vector_space: str,
    generation_id: int,
    extent: int,
    dimensions: int,
    seed: int,
    vector_dtype: str,
    readers: Sequence[int],
    limit: int,
    max_vectors: int,
    batch_size: int,
) -> dict[str, object]:
    physical_before = _sqlite_physical(database)
    fences_before = _owner_fences(database)
    explain = _explain(
        database,
        model_signature,
        generation_id,
        max_vectors=max_vectors,
    )
    rounds: dict[str, dict[str, object]] = {}
    for readers_count in readers:
        cold = _round(
            database,
            model_signature,
            vector_space,
            generation_id,
            dimensions,
            seed,
            readers=readers_count,
            limit=limit,
            max_vectors=max_vectors,
            batch_size=batch_size,
        )
        warm = [
            _round(
                database,
                model_signature,
                vector_space,
                generation_id,
                dimensions,
                seed,
                readers=readers_count,
                limit=limit,
                max_vectors=max_vectors,
                batch_size=batch_size,
            )
            for _ in range(WARM_REPEATS)
        ]
        warm_latencies = [
            float(observation["elapsed_seconds"])
            for round_value in warm
            for observation in round_value["observations"]
        ]
        rounds[str(readers_count)] = {
            "cold": cold,
            "warm": warm,
            "warm_latency_seconds": {
                "p50": _percentile(warm_latencies, 0.50),
                "p95": _percentile(warm_latencies, 0.95),
                "samples": len(warm_latencies),
            },
        }
    physical_after = _sqlite_physical(database)
    fences_after = _owner_fences(database)
    return {
        "model_signature": model_signature,
        "dimensions": dimensions,
        "vector_dtype": vector_dtype,
        "vector_blob_bytes": dimensions * (2 if vector_dtype == "float16" else 4),
        "generation_id": generation_id,
        "vectors_available": extent,
        "limit": limit,
        "max_vectors": max_vectors,
        "batch_size": batch_size,
        "schema_version": EXPECTED_SEMANTIC_SCHEMA_VERSION,
        "query": {
            "seed": seed,
            "dimensions": dimensions,
            "fingerprint_sha256": _query_fingerprint(dimensions, seed),
        },
        "coverage_expectation": {
            "scanned_fraction_at_limit": min(1.0, max_vectors / extent) if extent else 0.0,
            "full_scan_expected": max_vectors >= extent,
        },
        "readers": rounds,
        "explain_query_plan": explain,
        "sqlite_physical_before": physical_before,
        "sqlite_physical_after": physical_after,
        "read_only_file_delta": {
            key: int(physical_after["files"][key]) - int(physical_before["files"][key])
            for key in physical_before["files"]
        },
        "owner_fences": {
            "before": fences_before,
            "after": fences_after,
            "unchanged": fences_before == fences_after,
        },
    }


def _measurement_failure_reasons(
    measured: Mapping[str, object],
    *,
    extent: int,
    limit: int,
    max_vectors: int,
    readers: Sequence[int],
) -> list[str]:
    """Reject incomplete coverage, errors, and limit-truncated points."""

    reasons: list[str] = []
    if max_vectors < extent:
        reasons.append("max_vectors_below_extent")
    fences = measured.get("owner_fences")
    if not isinstance(fences, Mapping) or fences.get("unchanged") is not True:
        reasons.append("owner_fence_changed_or_missing")
    file_delta = measured.get("read_only_file_delta")
    if not isinstance(file_delta, Mapping) or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value != 0
        for value in file_delta.values()
    ):
        reasons.append("read_only_file_delta_nonzero_or_missing")
    plan = measured.get("explain_query_plan")
    if not isinstance(plan, list) or not plan:
        reasons.append("sql_plan_missing")
    rounds = measured.get("readers")
    if not isinstance(rounds, Mapping):
        return [*reasons, "reader_rounds_missing"]
    expected_hits = min(limit, extent)

    def inspect_observation(
        observation: object,
        *,
        reader_count: int,
        label: str,
        round_index: int,
        observation_index: int,
    ) -> None:
        prefix = f"reader_{reader_count}_{label}_{round_index}_{observation_index}"
        if not isinstance(observation, Mapping):
            reasons.append(f"{prefix}_invalid")
            return
        if observation.get("complete") is not True:
            reasons.append(f"{prefix}_incomplete")
        if observation.get("scanned") != extent:
            reasons.append(f"{prefix}_scan_mismatch")
        if observation.get("hits") != expected_hits:
            reasons.append(f"{prefix}_topk_count_mismatch")
        if observation.get("next_cursor") is not None:
            reasons.append(f"{prefix}_cursor_remaining")
        if not isinstance(observation.get("topk_fingerprint_sha256"), str):
            reasons.append(f"{prefix}_topk_fingerprint_missing")
        round_fences = observation.get("owner_fences")
        if round_fences is not None and (
            not isinstance(round_fences, Mapping)
            or round_fences.get("unchanged") is not True
        ):
            reasons.append(f"{prefix}_owner_fence_changed")

    for reader_count in readers:
        value = rounds.get(str(reader_count))
        if not isinstance(value, Mapping):
            reasons.append(f"reader_{reader_count}_missing")
            continue
        cold = value.get("cold")
        if not isinstance(cold, Mapping):
            reasons.append(f"reader_{reader_count}_cold_missing")
        else:
            cold_observations = cold.get("observations")
            if not isinstance(cold_observations, list) or len(cold_observations) != reader_count:
                reasons.append(f"reader_{reader_count}_cold_observation_count")
            elif not cold_observations:
                reasons.append(f"reader_{reader_count}_cold_missing")
            else:
                for observation_index, observation in enumerate(cold_observations):
                    inspect_observation(
                        observation,
                        reader_count=reader_count,
                        label="cold",
                        round_index=0,
                        observation_index=observation_index,
                    )

        warm = value.get("warm")
        if not isinstance(warm, list) or len(warm) != WARM_REPEATS:
            reasons.append(f"reader_{reader_count}_warm_round_count")
            warm_rounds: Sequence[object] = ()
        else:
            warm_rounds = warm
        warm_observation_count = 0
        for round_index, warm_round in enumerate(warm_rounds):
            if not isinstance(warm_round, Mapping):
                reasons.append(f"reader_{reader_count}_warm_{round_index}_invalid")
                continue
            warm_observations = warm_round.get("observations")
            if not isinstance(warm_observations, list) or len(warm_observations) != reader_count:
                reasons.append(f"reader_{reader_count}_warm_{round_index}_observation_count")
                continue
            warm_observation_count += len(warm_observations)
            for observation_index, observation in enumerate(warm_observations):
                inspect_observation(
                    observation,
                    reader_count=reader_count,
                    label="warm",
                    round_index=round_index,
                    observation_index=observation_index,
                )
        warm_latency = value.get("warm_latency_seconds")
        if not isinstance(warm_latency, Mapping) or not isinstance(
            warm_latency.get("p95"), (int, float)
        ):
            reasons.append(f"reader_{reader_count}_warm_p95_missing")
        elif warm_latency.get("samples") != warm_observation_count:
            reasons.append(f"reader_{reader_count}_warm_sample_count")
    return reasons


def _private_environment(root: Path) -> None:
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
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {name: str(directory) for name, directory in directories.items()}
    environment.update(
        {
            "HF_HUB_CACHE": str(root / "model-cache" / "huggingface" / "hub"),
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
    # Deliberately preserve an inherited audit-lab marker; it is not used as
    # a source or destination authority by this repo-native harness.
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


def _effective_temp_root(
    path: Path | None,
    *,
    repository_root: Path,
) -> tuple[Path, bool]:
    if path is None:
        return (
            Path(tempfile.mkdtemp(prefix="neocortex-exact-search-", dir=SYSTEM_TEMP_ROOT)),
            True,
        )
    if not path.is_absolute():
        raise ValueError("--temp-root must be absolute")
    selected = path.expanduser().resolve()
    if _path_is_within(selected, repository_root) or _path_is_within(repository_root, selected):
        raise ValueError("--temp-root must not overlap the checkout")
    home = Path.home().resolve()
    product_roots = (
        home / ".config" / "Neocortex",
        home / ".local" / "share" / "Neocortex",
        home / ".local" / "state" / "Neocortex",
        home / ".cache" / "Neocortex",
    )
    if any(_path_is_within(selected, root) or _path_is_within(root, selected) for root in product_roots):
        raise ValueError("--temp-root must not overlap installed NeoCortex state")
    selected.mkdir(parents=True, exist_ok=True, mode=0o700)
    return selected, False


def _output_path(
    path: Path | None,
    *,
    repository_root: Path,
    temp_root: Path,
) -> Path | None:
    if path is None or str(path) == "-":
        return None
    if not path.is_absolute():
        raise ValueError("--output/--report must be absolute or '-'")
    selected = path.expanduser().resolve()
    if _path_is_within(selected, repository_root):
        raise ValueError("--output/--report must be outside the checkout")
    if _path_is_within(selected, temp_root):
        raise ValueError("--output/--report must be outside --temp-root")
    if selected.exists() or selected.is_symlink():
        raise ValueError(f"refusing to overwrite existing report: {selected}")
    selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return selected


def _write_report(path: Path | None, encoded: str) -> None:
    if path is None:
        print(encoded)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise FileExistsError(f"temporary report path already exists: {temporary}")
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


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scales",
        nargs="+",
        type=int,
        default=None,
        help=(
            "synthetic vector counts; normal points are 100000/500000, while "
            "--canary permits only 100/1000"
        ),
    )
    parser.add_argument(
        "--canary",
        action="store_true",
        help="run only 100/1000 smoke points; never baseline or primary acceptance",
    )
    parser.add_argument(
        "--repository-root",
        "--repo-root",
        dest="repository_root",
        type=Path,
        default=DEFAULT_REPO_ROOT,
    )
    parser.add_argument(
        "--temp-root",
        "--work-root",
        dest="temp_root",
        type=Path,
        help="absolute private parent; default is a fresh system-temporary root",
    )
    parser.add_argument("--output", "--report", dest="output", type=Path, default=None)
    parser.add_argument("--model-signature", default=None)
    parser.add_argument("--readers", nargs="+", type=int, default=(1,))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-vectors", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    if args.seed < 0:
        parser.error("--seed must be nonnegative")
    allowed_scales = CANARY_SCALES if args.canary else NORMAL_SCALES
    if args.scales is None:
        args.scales = allowed_scales
    if (
        not args.scales
        or any(value not in allowed_scales for value in args.scales)
        or len(set(args.scales)) != len(args.scales)
    ):
        mode = "canary 100/1000" if args.canary else "normal 100000/500000"
        parser.error(f"--scales must be unique values from {mode}")
    if any(value not in {1, 2, 4} for value in args.readers):
        parser.error("--readers accepts only 1, 2 or 4")
    if len(set(args.readers)) != len(args.readers):
        parser.error("--readers cannot repeat a reader count")
    if not 1 <= args.limit <= 10_000:
        parser.error("--limit must be between 1 and 10000")
    if args.max_vectors is not None and not 1 <= args.max_vectors <= 10_000_000:
        parser.error("--max-vectors must be between 1 and 10000000")
    if not 1 <= args.batch_size <= 10_000:
        parser.error("--batch-size must be between 1 and 10000")
    if args.model_signature is not None and not args.model_signature.strip():
        parser.error("--model-signature cannot be blank")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repository_root = args.repository_root.expanduser().resolve()
    temp_parent: Path | None = None
    run_root: Path | None = None
    owns_temp_parent = False
    output_path: Path | None = None
    repository: Any | None = None
    original_database_factory: Any | None = None
    report: dict[str, object]
    exit_code = 0
    try:
        repository_root = _safe_absolute_path(
            repository_root,
            label="--repository-root",
            must_exist=True,
        )
        temp_parent, owns_temp_parent = _effective_temp_root(
            args.temp_root,
            repository_root=repository_root,
        )
        run_root = Path(
            tempfile.mkdtemp(
                prefix=f"semantic-exact-search-{os.getpid()}-",
                dir=str(temp_parent),
            )
        )
        _private_environment(run_root)
        output_path = _output_path(
            args.output,
            repository_root=repository_root,
            temp_root=temp_parent,
        )
        _load_repository(repository_root)
        from neocortex.semantic import semantic_search_repository as repository_module

        repository = repository_module
        selected_signature = args.model_signature or MODEL_SIGNATURE
        selected_model = _fixture_model(selected_signature)
        original_database_factory = repository_module.semantic_database
        repository_module.semantic_database = _traced_semantic_database(
            original_database_factory
        )
        results: list[dict[str, object]] = []
        for scale in args.scales:
            database = run_root / f"semantic-{scale}.sqlite3"
            point_started = time.perf_counter()
            try:
                build_started = time.perf_counter()
                generation_id = _build_synthetic_fixture(
                    database,
                    scale,
                    model_spec=selected_model,
                    model_signature=selected_model.model_signature,
                    dimensions=selected_model.dimensions,
                    seed=args.seed,
                )
                build_elapsed_seconds = time.perf_counter() - build_started
                max_vectors = args.max_vectors or min(DEFAULT_MAX_VECTORS, scale)
                measured = _measure(
                    database,
                    model_signature=selected_model.model_signature,
                    vector_space=selected_model.vector_space,
                    generation_id=generation_id,
                    extent=scale,
                    dimensions=selected_model.dimensions,
                    seed=args.seed,
                    vector_dtype=selected_model.vector_dtype.value,
                    readers=args.readers,
                    limit=args.limit,
                    max_vectors=max_vectors,
                    batch_size=args.batch_size,
                )
                failure_reasons = _measurement_failure_reasons(
                    measured,
                    extent=scale,
                    limit=args.limit,
                    max_vectors=max_vectors,
                    readers=args.readers,
                )
                measured["scale"] = scale
                measured["canary"] = bool(args.canary)
                measured["acceptance_scope"] = (
                    "canary_non_baseline_non_primary"
                    if args.canary
                    else "normal_baseline_primary_comparable"
                )
                measured["status"] = "complete" if not failure_reasons else "failed"
                measured["failure_reasons"] = failure_reasons
                measured["build_elapsed_seconds"] = build_elapsed_seconds
                measured["fixture_database_bytes"] = _file_bytes(database)
                measured["synthetic_quality_warning"] = (
                    "synthetic vectors measure search cost only; they do not measure retrieval quality"
                )
                measured["point_elapsed_seconds"] = time.perf_counter() - point_started
                results.append(measured)
            except Exception as exc:
                results.append(
                    {
                        "scale": scale,
                        "canary": bool(args.canary),
                        "acceptance_scope": (
                            "canary_non_baseline_non_primary"
                            if args.canary
                            else "normal_baseline_primary_comparable"
                        ),
                        "status": "error",
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc)[:1_000],
                        },
                        "point_elapsed_seconds": time.perf_counter() - point_started,
                    }
                )
        failed_scales = [
            result
            for result in results
            if result.get("status") != "complete"
        ]
        if failed_scales:
            exit_code = 1
        report = {
            "schema": FIXTURE_SCHEMA,
            "status": "complete" if not failed_scales else "failed",
            "kind": "synthetic_search_fixture",
            "canary": bool(args.canary),
            "acceptance_scope": (
                "canary_non_baseline_non_primary"
                if args.canary
                else "normal_baseline_primary_comparable"
            ),
            "primary_acceptance": not bool(args.canary),
            "repo_root": str(repository_root),
            "runtime": _runtime_metadata(),
            "seed": args.seed,
            "model_signature": selected_model.model_signature,
            "model_real": False,
            "model_loaded": False,
            "model_contract": {
                "vector_space": selected_model.vector_space,
                "model_id": selected_model.model_id,
                "model_version": selected_model.model_version,
                "provider_metadata_only": selected_model.provider,
                "dimensions": selected_model.dimensions,
                "vector_dtype": selected_model.vector_dtype.value,
            },
            "dimensions": selected_model.dimensions,
            "vector_dtype": selected_model.vector_dtype.value,
            "source_contract": {
                "mode": "repo-native-synthetic-fixture",
                "reference_preimage_sha256": REFERENCE_PREIMAGE_SHA256,
                "semantic_schema_version": EXPECTED_SEMANTIC_SCHEMA_VERSION,
                "chunking_signature": FIXTURE_CHUNKING_SIGNATURE,
                "text_template": (
                    "Transformador {index:08d}; mantenimiento preventivo, diagnóstico, "
                    "aislamiento y protección en patio eléctrico."
                ),
            },
            "query_contract": {
                "count": 1,
                "seed": args.seed,
                "dimensions": selected_model.dimensions,
                "fingerprint_sha256": _query_fingerprint(
                    selected_model.dimensions,
                    args.seed,
                ),
            },
            "search_contract": {
                "limit": args.limit,
                "batch_size": args.batch_size,
                "max_vectors": args.max_vectors,
                "readers": list(args.readers),
                "warm_repeats": WARM_REPEATS,
                "full_scan_required_for_success": True,
            },
            "scales": results,
            "constraints": {
                "max_experiment_seconds": 1_800,
                "max_total_seconds": 14_400,
                "max_rss_bytes": MAX_RSS_BYTES,
                "max_temporary_bytes": MAX_TEMP_BYTES,
            },
            "temporary_state": True,
            "environment_isolated": True,
        }
        encoded = json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        _write_report(output_path, encoded)
    finally:
        if repository is not None and original_database_factory is not None:
            repository.semantic_database = original_database_factory
        if run_root is not None:
            shutil.rmtree(run_root, ignore_errors=True)
        if owns_temp_parent and temp_parent is not None:
            shutil.rmtree(temp_parent, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
