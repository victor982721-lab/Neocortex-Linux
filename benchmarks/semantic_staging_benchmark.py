"""Compare legacy per-operation staging with the bounded persistent session."""
# region [00] Contexto del módulo
# Módulo: benchmarks/semantic_staging_benchmark.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
import time
import tracemalloc
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import xxhash
# endregion [01]

# region [02] Implementación

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from neocortex.semantic import semantic_generation_repository, semantic_item_repository, semantic_text_index  # noqa: E402
from neocortex.semantic.semantic_chunking import (  # noqa: E402
    TextChunkingConfig,
    iter_text_chunks,
)
from neocortex.semantic.semantic_generation_worker import batches  # noqa: E402
from neocortex.semantic.semantic_models import (  # noqa: E402
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
    SemanticItem,
    TextSection,
    fingerprint_text,
)
from neocortex.semantic.semantic_sources import TextSourceRecord  # noqa: E402
from neocortex.semantic.semantic_state import (  # noqa: E402
    enqueue_text_chunk_jobs,
    finalize_semantic_item_refresh,
    finalize_text_chunk_refresh,
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)


CHUNKING = TextChunkingConfig(
    max_chars=256,
    max_terms=64,
    overlap_chars=0,
    overlap_terms=0,
    min_natural_break_chars=32,
)


def _model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "staging-benchmark-model-v1",
        "staging-benchmark-space-v1",
        EmbeddingModality.TEXT,
        "fixture/staging-benchmark",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _records(count: int, sections_per_item: int) -> tuple[TextSourceRecord, ...]:
    output: list[TextSourceRecord] = []
    for item_index in range(count):
        identity = f"benchmark-{item_index:07d}"
        item = SemanticItem(
            item_id=f"item:pdf:{identity}",
            source_kind="pdf",
            source_identity=identity,
            identity_version="benchmark-v1",
            fingerprint=fingerprint_text(f"source:{identity}"),
            path=f"C:/benchmark/{identity}.pdf",
            provenance={"synthetic": True},
        )
        for section_index in range(sections_per_item):
            output.append(
                TextSourceRecord(
                    item,
                    TextSection(
                        "pdf_page",
                        str(section_index + 1),
                        f"Transformador {item_index}; página {section_index}; "
                        "mantenimiento, diagnóstico, aislamiento y protección.",
                        {"page": section_index + 1, "synthetic": True},
                    ),
                )
            )
    return tuple(output)


def _iterator(records: Sequence[TextSourceRecord]):
    def selected(_state: Path, _source_kind: str) -> Iterator[TextSourceRecord]:
        return iter(records)

    return selected


def _initialize(database: Path) -> int:
    model = _model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    return start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="staging-benchmark-run",
    )


def _legacy_stage(
    database: Path,
    generation_id: int,
    records: Sequence[TextSourceRecord],
) -> tuple[int, int, int]:
    source_items = chunks_staged = queued = 0
    groups = semantic_text_index.grouped_text_records(
        database.parent,
        "pdf",
        source_record_iterator=_iterator(records),
    )
    for item_id, grouped in groups:
        iterator = iter(grouped)
        first = next(iterator)
        upsert_semantic_item(database, first.item, refresh_token="benchmark-refresh")
        source_items += 1
        sections = itertools.chain(
            (first.section,),
            (record.section for record in iterator),
        )
        chunks = iter_text_chunks(item_id, sections, CHUNKING)
        for batch in batches(chunks, semantic_text_index.STAGING_BATCH_SIZE):
            chunks_staged += stage_text_chunks(
                database,
                batch,
                refresh_token="benchmark-refresh",
                batch_size=semantic_text_index.STAGING_BATCH_SIZE,
            )
            queued += enqueue_text_chunk_jobs(
                database,
                generation_id,
                (chunk.chunk_id for chunk in batch),
                batch_size=semantic_text_index.STAGING_BATCH_SIZE,
            )
        finalize_text_chunk_refresh(
            database,
            item_id=item_id,
            chunking_signature=CHUNKING.signature,
            refresh_token="benchmark-refresh",
        )
    finalize_semantic_item_refresh(
        database,
        source_kind="pdf",
        refresh_token="benchmark-refresh",
    )
    return source_items, chunks_staged, queued


@contextmanager
def _connection_counter():
    modules = (
        semantic_text_index,
        semantic_item_repository,
        semantic_generation_repository,
    )
    originals = tuple(module.semantic_database for module in modules)
    observed = {"count": 0}

    def wrapper(original):
        @contextmanager
        def counted(*args, **kwargs):
            observed["count"] += 1
            with original(*args, **kwargs) as connection:
                yield connection

        return counted

    try:
        for module, original in zip(modules, originals, strict=True):
            module.semantic_database = wrapper(original)
        yield observed
    finally:
        for module, original in zip(modules, originals, strict=True):
            module.semantic_database = original


def _logical_digest(database: Path) -> str:
    queries = (
        """SELECT item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            provenance_json,source_revision_json,refresh_token,active
        FROM semantic_items ORDER BY item_id""",
        """SELECT chunk_id,item_id,ordinal,section_kind,section_id,start_char,
            end_char,text_zlib,text_chars,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,chunking_signature,provenance_json,
            refresh_token,active
        FROM text_chunks ORDER BY chunk_id""",
        """SELECT generation_id,model_signature,role,entity_kind,entity_id,
            item_id,content_xxh3_128,content_bytes,content_xxh3_64_guard,status,
            attempts,max_attempts,lease_owner,lease_until_ns,error_type,error_message
        FROM embedding_jobs ORDER BY entity_kind,entity_id""",
    )
    digest = xxhash.xxh3_128()
    with semantic_database(database, readonly=True) as connection:
        for query in queries:
            for row in connection.execute(query):
                digest.update(repr(tuple(row)).encode("utf-8"))
                digest.update(b"\0")
    return digest.hexdigest()


def _validate(database: Path) -> dict[str, object]:
    with semantic_database(database, readonly=True) as connection:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("semantic_items", "text_chunks", "embedding_jobs")
        }
    return {
        "integrity": integrity,
        "foreign_key_errors": foreign_keys,
        "counts": counts,
        "logical_xxh3_128": _logical_digest(database),
    }


def _database_bytes(database: Path) -> dict[str, int]:
    return {
        suffix or "db": path.stat().st_size if path.exists() else 0
        for suffix, path in (
            ("", database),
            ("wal", Path(f"{database}-wal")),
            ("shm", Path(f"{database}-shm")),
        )
    }


def _run_once(
    root: Path,
    mode: str,
    run_index: int,
    records: Sequence[TextSourceRecord],
) -> dict[str, object]:
    database = root / f"{mode}-{run_index}.sqlite3"
    generation_id = _initialize(database)
    tracemalloc.start()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    with _connection_counter() as connections:
        if mode == "legacy":
            result = _legacy_stage(database, generation_id, records)
        else:
            result = semantic_text_index._stage_source(
                database,
                database.parent,
                "pdf",
                generation_id=generation_id,
                refresh_token="benchmark-refresh",
                chunking=CHUNKING,
                source_record_iterator=_iterator(records),
            )
    cpu_seconds = time.process_time() - cpu_start
    wall_seconds = time.perf_counter() - wall_start
    _, peak_heap = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "mode": mode,
        "run": run_index,
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "peak_python_heap_bytes": peak_heap,
        "write_connections": connections["count"],
        "stage_result": result,
        "database_bytes": _database_bytes(database),
        "validation": _validate(database),
    }


def _median(results: Sequence[dict[str, object]], key: str) -> float:
    values: list[float] = []
    for result in results:
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"benchmark result {key!r} must be numeric")
        values.append(float(value))
    return statistics.median(values)


def _result_digest(result: Mapping[str, object]) -> str:
    validation = result.get("validation")
    if not isinstance(validation, Mapping):
        raise TypeError("benchmark validation result must be a mapping")
    digest = validation.get("logical_xxh3_128")
    if not isinstance(digest, str):
        raise TypeError("benchmark logical digest must be text")
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=1_000)
    parser.add_argument("--sections-per-item", type=int, default=1)
    parser.add_argument("--runs", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.items <= 0 or arguments.sections_per_item <= 0:
        parser.error("items and sections-per-item must be positive")
    if arguments.runs <= 0:
        parser.error("runs must be positive")

    records = _records(arguments.items, arguments.sections_per_item)
    results: list[dict[str, object]] = []
    with TemporaryDirectory(prefix="neocortex-semantic-staging-") as temporary:
        root = Path(temporary)
        for run_index in range(arguments.runs):
            modes = ("candidate", "legacy") if run_index % 2 == 0 else (
                "legacy",
                "candidate",
            )
            for mode in modes:
                results.append(_run_once(root, mode, run_index, records))

    by_mode = {
        mode: tuple(result for result in results if result["mode"] == mode)
        for mode in ("legacy", "candidate")
    }
    legacy_digest = {
        _result_digest(result) for result in by_mode["legacy"]
    }
    candidate_digest = {
        _result_digest(result) for result in by_mode["candidate"]
    }
    if legacy_digest != candidate_digest or len(legacy_digest) != 1:
        raise RuntimeError("legacy and candidate logical projections differ")

    legacy_wall = _median(by_mode["legacy"], "wall_seconds")
    candidate_wall = _median(by_mode["candidate"], "wall_seconds")
    legacy_connections = _median(by_mode["legacy"], "write_connections")
    candidate_connections = _median(by_mode["candidate"], "write_connections")
    report = {
        "benchmark_version": 1,
        "items": arguments.items,
        "sections_per_item": arguments.sections_per_item,
        "runs": arguments.runs,
        "chunking_signature": CHUNKING.signature,
        "results": results,
        "summary": {
            "legacy_median_wall_seconds": legacy_wall,
            "candidate_median_wall_seconds": candidate_wall,
            "wall_speedup": legacy_wall / candidate_wall,
            "legacy_median_write_connections": legacy_connections,
            "candidate_median_write_connections": candidate_connections,
            "write_connection_reduction_fraction": (
                1.0 - candidate_connections / legacy_connections
            ),
            "logical_xxh3_128": next(iter(legacy_digest)),
        },
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# endregion [02]
