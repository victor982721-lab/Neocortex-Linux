#!/usr/bin/env python3
"""Bounded synthetic conformance/measurement through SemanticVectorSearch.

Uses the existing 768-dimensional fixture; no encoder or user corpus is read.
The output records the exact oracle, scan coverage, fallback and snapshot
preparation separately. This is a development benchmark, never runtime logic.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import tempfile
import time
from typing import Any
from unittest.mock import patch

from benchmarks import semantic_exact_search_benchmark as fixture
from neocortex.semantic import semantic_exact_index_format as index_format
from neocortex.semantic import semantic_search_repository as repository
from neocortex.semantic.semantic_exact_index import (
    ExactIndexUnavailable, PersistedExactVectorSearch, open_exact_index, prepare_exact_index,
)
from neocortex.semantic.semantic_models import EmbeddingModality, ExactSearchQuery
from neocortex.semantic.semantic_schema import SemanticReadContext, semantic_read_context


def _page_payload(page: Any) -> dict[str, object]:
    return {
        "hits": [
            [hit.ref_id, hit.item_id, hit.entity_id, hit.indexed_model_signature,
             hit.generation_id, hit.score.hex(), dict(hit.provenance)]
            for hit in page.hits
        ],
        "scanned": page.scanned, "next_cursor": page.next_cursor, "complete": page.complete,
    }


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _percentiles(values: list[float]) -> dict[str, object]:
    return {
        "samples_seconds": values, "p50_seconds": statistics.median(values),
        "p95_seconds": sorted(values)[min(len(values) - 1, int(len(values) * .95))],
        "p95_method": "nearest-rank; small synthetic sample, not a service SLO",
    }


def measure_size(root: Path, count: int, repeats: int, deadline: float) -> dict[str, object]:
    def cancelled() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("synthetic vector benchmark deadline")

    root.mkdir(mode=0o700)
    model = fixture._fixture_model(fixture.MODEL_SIGNATURE)
    database = root / "owner.sqlite3"
    started = time.monotonic()
    fixture._build_synthetic_fixture(
        database, count, model_spec=model, model_signature=model.model_signature,
        dimensions=model.dimensions, seed=fixture.DEFAULT_SEED,
    )
    fixture_seconds = time.monotonic() - started
    cancelled()
    query = ExactSearchQuery(
        model.model_signature, model.vector_space, model.dimensions,
        fixture._query_vector(model.dimensions, fixture.DEFAULT_SEED), EmbeddingModality.TEXT,
        (model.model_signature,),
    )
    directory = root / "index"
    started = time.monotonic()
    prepare_exact_index(
        database, directory, model_signature=model.model_signature,
        text_scope="all", cancellation_check=cancelled,
    ).close()
    build_seconds = time.monotonic() - started
    started = time.monotonic()
    handle = open_exact_index(database, directory, cancellation_check=cancelled)
    open_seconds = time.monotonic() - started
    backend = PersistedExactVectorSearch(handle)
    counters = {"native_reader_opens": 0, "native_binary_hash_calls": 0}
    native_database = repository.semantic_database
    native_hash = index_format._numpy_core_sha256

    @contextmanager
    def measured_database(path: Path, **kwargs: Any) -> Any:
        counters["native_reader_opens"] += 1
        with native_database(path, **kwargs) as connection:
            yield connection

    def measured_hash(path: Path, **kwargs: Any) -> str:
        counters["native_binary_hash_calls"] += 1
        return native_hash(path, **kwargs)

    trials: list[dict[str, object]] = []
    try:
        with patch.object(repository, "semantic_database", measured_database), patch.object(index_format, "_numpy_core_sha256", measured_hash):
            for evidence_mode in (False, True):
                search = repository.search_exact_evidence_page if evidence_mode else repository.search_exact_page
                for name, settings in (
                    ("small_k", {"limit": 5}), ("large_k", {"limit": 128}),
                    ("partial", {"limit": 20, "max_vectors": count // 2}),
                    ("cursor", {"limit": 20, "after_ref_id": count // 2}),
                    ("empty_title_filter", {"limit": 20, "text_scope": "title"}),
                    ("diagnostics", {"limit": 20, "diagnostic_item_ids": ("diagnostic-absent",)}),
                ):
                    options = {"limit": 20, "max_vectors": count, "batch_size": 128, "cancellation_check": cancelled, **settings}
                    before_io = fixture._read_proc_io()
                    before_counters = dict(counters)
                    times: dict[str, list[float]] = {"native": [], "persisted": []}
                    metadata: dict[str, object] = {}
                    exact_equal = True
                    oracle = None
                    selected = None
                    for _ in range(repeats):
                        started = time.monotonic()
                        oracle = search(database, query, **options)
                        times["native"].append(time.monotonic() - started)
                        started = time.monotonic()
                        selected = search(database, query, **options, vector_backend=backend, backend_diagnostics=metadata)
                        times["persisted"].append(time.monotonic() - started)
                        exact_equal &= _page_payload(oracle) == _page_payload(selected)
                    assert oracle is not None and selected is not None
                    expected_ids = {hit.ref_id for hit in oracle.hits}
                    actual_ids = {hit.ref_id for hit in selected.hits}
                    after_io = fixture._read_proc_io()
                    trials.append({
                        "case": name, "evidence_mode": evidence_mode, "settings": {k:v for k,v in options.items() if k != "cancellation_check"},
                        "native": _percentiles(times["native"]), "persisted": _percentiles(times["persisted"]),
                        "exact_page_equal": exact_equal, "ordered_page_sha256": _digest(_page_payload(selected)),
                        "recall_at_k": len(expected_ids & actual_ids) / len(expected_ids) if expected_ids else None,
                        "recall_denominator": len(expected_ids), "matched_ids": len(expected_ids & actual_ids),
                        "selected_scan_rows": selected.scanned, "fixture_rows": count,
                        "scan_fraction": selected.scanned / count, "backend": metadata,
                        "instrumentation": {key: counters[key] - before_counters[key] for key in counters},
                        "process_io_delta": {key: after_io[key] - before_io[key] for key in after_io} if before_io and after_io else None,
                    })
                    if not exact_equal:
                        raise AssertionError(f"backend differed from exact oracle: {name}")
        snapshots = []
        # Force the existing detached-read policy only for this synthetic
        # measurement, to expose fresh/reused copy costs instead of assuming
        # all ordinary (usually immutable) queries copy their owner.
        @contextmanager
        def detached_database(path: Path, **kwargs: Any) -> Any:
            kwargs["read_mode"] = "snapshot_temp"
            with native_database(path, **kwargs) as connection:
                yield connection
        with patch.object(repository, "semantic_database", detached_database):
            with semantic_read_context(SemanticReadContext(cancellation_check=cancelled)) as context:
                for phase in ("new", "reused"):
                    before = dict(context.metrics)
                    started = time.monotonic()
                    repository.search_exact_page(database, query, limit=5, max_vectors=count, cancellation_check=cancelled)
                    snapshots.append({"phase": phase, "query_seconds": time.monotonic() - started, "before": before, "after": dict(context.metrics)})
                info = database.stat()
                os.utime(database, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
                try:
                    repository.search_exact_page(database, query, limit=5, max_vectors=count, cancellation_check=cancelled)
                except repository.SemanticStateError as exc:
                    snapshots.append({"phase": "invalidated", "outcome": type(exc).__name__, "implicit_retry": False})
                else:
                    raise AssertionError("changed owner reused a captured snapshot")
        # The stale prepared handle must decline before scanning. A *new*
        # public query captures the modified owner and may use native exact.
        stale_metadata: dict[str, object] = {}
        repository.search_exact_page(database, query, vector_backend=backend, backend_diagnostics=stale_metadata)
        if stale_metadata.get("backend_id") != "native_exact" or not stale_metadata.get("fallback_reason"):
            raise AssertionError("stale handle did not report its pre-scan fallback")
        cancellation_count = 0
        def interrupt() -> None:
            nonlocal cancellation_count
            cancellation_count += 1
            if cancellation_count >= 3:
                raise InterruptedError("synthetic cancellation")
        try:
            repository.search_exact_page(database, query, cancellation_check=interrupt)
        except InterruptedError:
            pass
        else:
            raise AssertionError("cancellation was swallowed")
        interrupted_directory = root / "interrupted-index"
        def cancel_publication() -> None:
            cancelled()
            if (interrupted_directory / "rows.bin").exists():
                raise InterruptedError("synthetic interruption during artifact publication")
        interrupted_started = time.monotonic()
        try:
            prepare_exact_index(
                database, interrupted_directory, model_signature=model.model_signature,
                text_scope="all", cancellation_check=cancel_publication,
            ).close()
        except InterruptedError:
            pass
        else:
            raise AssertionError("interrupted publication unexpectedly completed")
        interrupted_seconds = time.monotonic() - interrupted_started
        if (interrupted_directory / "exact-index-ready.json").exists():
            raise AssertionError("interrupted artifact retained a publication marker")
        try:
            open_exact_index(database, interrupted_directory, cancellation_check=cancelled)
        except (ExactIndexUnavailable, FileNotFoundError):
            pass
        else:
            raise AssertionError("interrupted artifact was accepted by open")
        started = time.monotonic()
        prepare_exact_index(database, root / "rebuilt-index", model_signature=model.model_signature, text_scope="all", cancellation_check=cancelled).close()
        rebuild_seconds = time.monotonic() - started
        return {
            "rows": count, "dimensions": model.dimensions, "seed": fixture.DEFAULT_SEED,
            "fixture_seconds": fixture_seconds, "artifact_build_seconds": build_seconds,
            "artifact_open_seconds": open_seconds, "artifact_rebuild_seconds": rebuild_seconds,
            "interrupted_publication": {"seconds": interrupted_seconds, "ready_marker": False, "open_rejected": True, "recovery": "explicit build to a new destination"},
            "owner_bytes": database.stat().st_size,
            "artifact_bytes": sum(path.stat().st_size for path in directory.iterdir() if path.is_file()),
            "trials": trials, "snapshots": snapshots, "stale_new_query": stale_metadata,
            "cancellation_callback_count": cancellation_count,
            "rss_process_highwater_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        }
    finally:
        backend.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1024, 10_240])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 2 <= args.repeats <= 20 or not 0 < args.timeout_seconds <= 900 or any(not 128 <= n <= 100_000 or n % 256 for n in args.sizes):
        parser.error("sizes must be multiples of 256 up to 100000; repeats 2..20; timeout <=900")
    repo = Path(__file__).resolve().parents[1]
    output = args.output.absolute()
    if not args.output.is_absolute() or output.is_relative_to(repo) or output.exists():
        parser.error("output must be a new absolute file outside the checkout")
    deadline = time.monotonic() + args.timeout_seconds
    with tempfile.TemporaryDirectory(prefix="neocortex-vector-benchmark-") as temporary:
        root = Path(temporary)
        fixture._private_environment(root)
        values = [measure_size(root / str(count), count, args.repeats, deadline) for count in args.sizes]
    report = {
        "schema": "neocortex.semantic-vector-backend-benchmark/v1", "python": platform.python_version(),
        "arguments": vars(args) | {"output": str(output)}, "sizes": values,
        "source_sha256": {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (Path(__file__), Path(repository.__file__), Path(index_format.__file__))},
        "decision": "retain_exact; no synthetic evidence here justifies ANN",
        "limits": ["synthetic vectors do not measure embedding relevance", "no user corpus, encoder, or personal-lineage acceptance", "cold OS cache was not forced; first-call and replay costs are separate", "RSS is process high-water, not per-trial allocation", "small benchmark; not a universal ANN decision"],
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
