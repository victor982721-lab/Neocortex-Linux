#!/usr/bin/env python3
"""Bounded, temporary-state Semantic generation control benchmark.

This harness exercises the real Semantic staging/generation repositories with
the repository's deterministic test backend.  It deliberately does not read
the live corpus or any configured state directory.  Every point gets a fresh
temporary SQLite owner and is flushed to a JSONL receipt before the next point
starts.

The deterministic backend is a contract fixture, not a quality or real-model
throughput measurement.  The resulting numbers are useful for SQL, lineage,
batching, idempotence and incremental-work comparisons.  This module is
deliberately repository-native: it imports the explicit checkout and its
deterministic test backend only after the command line has selected an
isolated temporary root.  It has no audit-tree or installed-state dependency.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _datetime
import hashlib
import json
import os
import re
import resource
import shutil
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


# Do not create checkout __pycache__ files merely by importing the exact source
# selected by the coordinator.
sys.dont_write_bytecode = True


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORY_ROOT = REPOSITORY_ROOT
# Keep the audit scale command reproducible while making the P1 nominal point
# explicit: 2,500 synthetic items produce exactly 5,000 jobs through two
# emitted chunks per item (source body plus metadata title).
DEFAULT_SIZES = (100, 1_000, 2_000, 5_000, 10_000)
SUPPORTED_SIZES = (100, 250, 1_000, 2_000, 5_000, 10_000)
DEFAULT_POINT_SECONDS = 120.0
MAX_POINT_SECONDS = 900.0
DEFAULT_GROUP_SECONDS = 1_800.0
MAX_GROUP_SECONDS = 1_800.0
EXTERNAL_MEMORY_CAP_BYTES = 4_000_000_000
EXTERNAL_TEMP_CAP_BYTES = 20_000_000_000
BENCHMARK_SCHEMA = "neocortex.semantic-generation-benchmark/v1"
FIXTURE_BACKEND_CONTRACT = "tests.semantic_test_backend.DeterministicTestBackend"
FIXTURE_BACKEND_BATCH_SIZE = 32
SOURCE_KIND = "pdf"
BASE_PROCESSING_SIGNATURE = "semantic-audit-generation-v1"
LOGICAL_INTEGRITY_SCHEMA = "neocortex.semantic-generation-logical-integrity/v1"
SNAPSHOT_SCHEMA = "neocortex.semantic-generation-snapshot/v1"
SYSTEM_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
EXPECTED_NOMINAL_ITEMS = 2_500
EXPECTED_NOMINAL_JOBS = 5_000
LIFECYCLE_ORDER = (
    "initialize",
    "register_model",
    "start_generation",
    "stage_source",
    "prepare_generation",
    "run_generation",
    "replay",
)
SNAPSHOT_SIDECARS = ("-wal", "-shm", "-journal")
CASE_NAMES = (
    "identical",
    "metadata",
    "smallchange",
    "move",
    "rename",
    "delete",
    "model_signature_change",
    "chunking_signature_change",
)

# These fields are operational timestamps/lease details, not Semantic identity.
# Excluding them makes a logical successor/replay comparison stable while the
# raw phase timers and SQL counters remain untouched.  Source values are never
# emitted: only typed-cell/row digests and cardinalities leave this harness.
_VOLATILE_LOGICAL_COLUMNS = frozenset(
    {
        "available_ns",
        "attempt_started_ns",
        "attempt_sequence",
        "attempts",
        "duration_ns",
        "lease_owner",
        "lease_until_ns",
        "refresh_token",
        "started_ns",
        "finished_ns",
        "committed_ns",
        "created_ns",
        "updated_ns",
        "captured_ns",
    }
)

# Ten bounded referential-integrity probes.  They are intentionally structural
# and return only counts; no SQL text or source values are included in receipts.
_ORPHAN_CHECKS = (
    ("jobs_generation", "embedding_jobs", "generation_id", "embedding_generations", "generation_id"),
    ("generations_model", "embedding_generations", "model_signature", "embedding_models", "model_signature"),
    ("generation_members_generation", "embedding_generation_members", "generation_id", "embedding_generations", "generation_id"),
    ("generation_members_payload", "embedding_generation_members", "payload_id", "vector_payloads", "payload_id"),
    ("generation_members_item", "embedding_generation_members", "item_id", "semantic_items", "item_id"),
    ("generation_members_item_revision", "embedding_generation_members", "item_revision_id", "semantic_item_revisions", "item_revision_id"),
    ("generation_members_chunk_revision", "embedding_generation_members", "chunk_revision_id", "semantic_chunk_revisions", "chunk_revision_id"),
    ("published_heads_generation", "published_embedding_heads", "generation_id", "embedding_generations", "generation_id"),
    ("published_heads_model", "published_embedding_heads", "model_signature", "embedding_models", "model_signature"),
    ("text_embeddings_payload", "text_embeddings", "payload_id", "vector_payloads", "payload_id"),
)


class BenchmarkConfigurationError(ValueError):
    """The requested point is outside the bounded temporary contract."""


@dataclass(frozen=True, slots=True)
class LoadedComponents:
    generation_repository: Any
    generation_worker: Any
    item_repository: Any
    semantic_schema: Any
    text_index: Any
    DeterministicTestBackend: Any
    EmbeddingModality: Any
    EmbeddingModelSpec: Any
    EmbeddingRole: Any
    SemanticItem: Any
    TextChunkingConfig: Any
    TextSection: Any
    TextSourceRecord: Any
    fingerprint_text: Any
    SemanticWorkBudget: Any
    initialize_semantic_state: Any
    register_embedding_model: Any
    start_embedding_generation: Any
    prepare_embedding_generation: Any
    generation_summary: Any
    run_generation: Any


COMPONENTS: LoadedComponents | None = None


def _load_components(repository_root: Path) -> LoadedComponents:
    """Import one explicit checkout; no installed package is selected silently."""

    root = Path(repository_root).expanduser().resolve()
    if not (root / "neocortex" / "semantic").is_dir():
        raise BenchmarkConfigurationError(f"repository root is not NeoCortex: {root}")
    sys.path.insert(0, os.fspath(root))
    try:
        from tests.semantic_test_backend import DeterministicTestBackend
        from neocortex.semantic import semantic_generation_repository as generation_repository
        from neocortex.semantic import semantic_generation_worker as generation_worker
        from neocortex.semantic import semantic_item_repository as item_repository
        from neocortex.semantic import semantic_schema
        from neocortex.semantic import semantic_text_index as text_index
        from neocortex.semantic.semantic_chunking import TextChunkingConfig
        from neocortex.semantic.semantic_models import (
            EmbeddingModality,
            EmbeddingModelSpec,
            EmbeddingRole,
            SemanticItem,
            TextSection,
            fingerprint_text,
        )
        from neocortex.semantic.semantic_sources import TextSourceRecord
        from neocortex.semantic.semantic_state import (
            generation_summary,
            initialize_semantic_state,
            prepare_embedding_generation,
            register_embedding_model,
            start_embedding_generation,
        )
        from neocortex.semantic.semantic_generation_worker import run_generation
        from neocortex.semantic.semantic_work_budget import SemanticWorkBudget
    except Exception as exc:  # import failures are configuration, not a point result
        raise BenchmarkConfigurationError(
            f"could not import the selected checkout {root}: {type(exc).__name__}: {exc}"
        ) from exc
    return LoadedComponents(
        generation_repository,
        generation_worker,
        item_repository,
        semantic_schema,
        text_index,
        DeterministicTestBackend,
        EmbeddingModality,
        EmbeddingModelSpec,
        EmbeddingRole,
        SemanticItem,
        TextChunkingConfig,
        TextSection,
        TextSourceRecord,
        fingerprint_text,
        SemanticWorkBudget,
        initialize_semantic_state,
        register_embedding_model,
        start_embedding_generation,
        prepare_embedding_generation,
        generation_summary,
        run_generation,
    )


def _components() -> LoadedComponents:
    if COMPONENTS is None:  # pragma: no cover - guarded by main
        raise RuntimeError("benchmark components were not loaded")
    return COMPONENTS


class CountingDeterministicBackend:
    """Count calls while delegating vector generation to the existing fixture."""

    def __init__(
        self,
        components: LoadedComponents,
        model: Any,
        *,
        batch_size: int = FIXTURE_BACKEND_BATCH_SIZE,
    ) -> None:
        self._delegate = components.DeterministicTestBackend(model, batch_size=batch_size)
        self.calls = 0
        self.requests = 0
        self.input_bytes = 0
        self.batch_sizes: list[int] = []
        self.token_count_calls = 0
        self.token_count_inputs = 0
        self.elapsed_ns = 0

    @property
    def model(self) -> Any:
        return self._delegate.model

    @property
    def max_batch_size(self) -> int:
        return self._delegate.max_batch_size

    def embed(self, requests: Sequence[Any]) -> Sequence[Any]:
        started = time.perf_counter_ns()
        self.calls += 1
        self.requests += len(requests)
        self.batch_sizes.append(len(requests))
        self.input_bytes += sum(
            len(request.text.encode("utf-8"))
            for request in requests
            if request.text is not None
        )
        try:
            return self._delegate.embed(requests)
        finally:
            self.elapsed_ns += time.perf_counter_ns() - started

    def text_token_counts(self, texts: Sequence[str]) -> tuple[tuple[int, ...], int]:
        self.token_count_calls += 1
        self.token_count_inputs += len(texts)
        return self._delegate.text_token_counts(texts)

    def text_tokenizer_contract(self) -> tuple[str, int]:
        return self._delegate.text_tokenizer_contract()


class SQLTrace:
    """Bounded SQL template counters; literal values never enter the receipt."""

    _quoted = re.compile(r"'(?:''|[^'])*'")
    _number = re.compile(r"(?<![A-Za-z_])\d+(?![A-Za-z_])")

    def __init__(self) -> None:
        self.total = 0
        self.reads = 0
        self.writes = 0
        self.begin_statements = 0
        self.commit_statements = 0
        self.rollback_statements = 0
        self.savepoint_statements = 0
        self.release_savepoint_statements = 0
        self.rollback_to_savepoint_statements = 0
        self.progress_handler_calls = 0
        self.progress_handler_instructions = 1_000
        self.templates: Counter[str] = Counter()

    def observe_progress(self) -> int:
        """Count SQLite VM progress callbacks without changing cancellation."""

        self.progress_handler_calls += 1
        return 0

    @classmethod
    def template(cls, statement: str) -> str:
        normalized = " ".join(statement.strip().split())
        if not normalized:
            return ""
        normalized = cls._quoted.sub("'?'", normalized)
        normalized = cls._number.sub("?", normalized)
        return normalized[:600]

    def observe(self, statement: str) -> None:
        template = self.template(statement)
        if not template:
            return
        self.total += 1
        verb = template.split(" ", 1)[0].upper()
        if verb == "BEGIN":
            self.begin_statements += 1
        elif verb == "COMMIT":
            self.commit_statements += 1
        elif verb == "ROLLBACK":
            if template.upper().startswith("ROLLBACK TO SAVEPOINT"):
                self.rollback_to_savepoint_statements += 1
            else:
                self.rollback_statements += 1
        elif verb == "SAVEPOINT":
            self.savepoint_statements += 1
        elif verb == "RELEASE":
            self.release_savepoint_statements += 1
        if verb in {"SELECT", "WITH", "EXPLAIN", "PRAGMA"}:
            self.reads += 1
        elif verb in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            self.writes += 1
        self.templates[template] += 1

    def as_dict(self) -> dict[str, object]:
        return {
            "total_statements": self.total,
            "read_statements": self.reads,
            "write_statements": self.writes,
            "transaction_begin_statements": self.begin_statements,
            "transaction_commit_statements": self.commit_statements,
            "transaction_rollback_statements": self.rollback_statements,
            "savepoint_statements": self.savepoint_statements,
            "release_savepoint_statements": self.release_savepoint_statements,
            "rollback_to_savepoint_statements": self.rollback_to_savepoint_statements,
            # A transaction is counted only once it is durably closed.  The
            # benchmark keeps BEGIN and COMMIT separate because SQLite may
            # open an implicit transaction for an API call.
            "transactions": self.commit_statements + self.rollback_statements,
            "commits": self.commit_statements,
            "template_count": len(self.templates),
            "sqlite_progress_handler_calls": self.progress_handler_calls,
            "sqlite_progress_vm_instructions_estimate": (
                self.progress_handler_calls * self.progress_handler_instructions
            ),
            "top_templates": [
                {"template": template, "count": count}
                for template, count in self.templates.most_common(24)
            ],
        }

    def counters(self) -> tuple[int, ...]:
        return (
            self.total,
            self.reads,
            self.writes,
            self.begin_statements,
            self.commit_statements,
            self.rollback_statements,
            self.savepoint_statements,
            self.release_savepoint_statements,
            self.rollback_to_savepoint_statements,
            self.progress_handler_calls,
        )

    def delta_since(self, prior: tuple[int, ...]) -> dict[str, int]:
        current = self.counters()
        return {
            "total_statements": current[0] - prior[0],
            "read_statements": current[1] - prior[1],
            "write_statements": current[2] - prior[2],
            "transaction_begin_statements": current[3] - prior[3],
            "transaction_commit_statements": current[4] - prior[4],
            "transaction_rollback_statements": current[5] - prior[5],
            "savepoint_statements": current[6] - prior[6],
            "release_savepoint_statements": current[7] - prior[7],
            "rollback_to_savepoint_statements": current[8] - prior[8],
            "transactions": (
                current[4]
                + current[5]
                - prior[4]
                - prior[5]
            ),
            "commits": current[4] - prior[4],
            "sqlite_progress_handler_calls": current[9] - prior[9],
            "sqlite_progress_vm_instructions_estimate": (
                current[9] - prior[9]
            ) * self.progress_handler_instructions,
        }


@dataclass(slots=True)
class _ProgressRegistration:
    callback: Callable[[], int]
    instructions: int


@dataclass(slots=True)
class _TraceConnectionState:
    """Callback ownership for one live connection, including nested wrappers."""

    connection: Any
    trace_refs: list[tuple[SQLTrace, int]] = field(default_factory=list)
    refcount: int = 0
    progress_stack: list[_ProgressRegistration] = field(default_factory=list)

    def add_trace(self, trace: SQLTrace) -> None:
        for index, (selected, count) in enumerate(self.trace_refs):
            if selected is trace:
                self.trace_refs[index] = (selected, count + 1)
                return
        self.trace_refs.append((trace, 1))

    def remove_trace(self, trace: SQLTrace) -> None:
        for index, (selected, count) in enumerate(self.trace_refs):
            if selected is not trace:
                continue
            if count <= 1:
                self.trace_refs.pop(index)
            else:
                self.trace_refs[index] = (selected, count - 1)
            return

    def observe_sql(self, statement: str) -> None:
        for trace, _count in tuple(self.trace_refs):
            trace.observe(statement)

    def observe_progress(self) -> int:
        for trace, _count in tuple(self.trace_refs):
            trace.observe_progress()
        return 0

    def current_progress(self) -> _ProgressRegistration:
        if self.progress_stack:
            return self.progress_stack[-1]
        instructions = self.trace_refs[0][0].progress_handler_instructions
        return _ProgressRegistration(self.observe_progress, instructions)


_TRACE_CONNECTION_STATES: dict[int, _TraceConnectionState] = {}


def _closed_connection_error(error: BaseException) -> bool:
    return isinstance(error, sqlite3.ProgrammingError) and "closed" in str(error).casefold()


def _set_progress_safely(
    state: _TraceConnectionState,
    registration: _ProgressRegistration,
    *,
    primary_error: BaseException | None,
) -> None:
    try:
        state.connection.set_progress_handler(registration.callback, registration.instructions)
    except BaseException as cleanup_error:
        if _closed_connection_error(cleanup_error):
            return
        if primary_error is None:
            raise
        primary_error.add_note(
            "benchmark progress-handler restoration failed: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )


def _register_trace_connection(
    connection: Any,
    trace: SQLTrace,
) -> _TraceConnectionState:
    key = id(connection)
    prior = _TRACE_CONNECTION_STATES.get(key)
    if prior is not None and prior.connection is not connection:
        _TRACE_CONNECTION_STATES.pop(key, None)
        prior = None
    if prior is None:
        state = _TraceConnectionState(connection)
        _TRACE_CONNECTION_STATES[key] = state
        try:
            connection.set_trace_callback(state.observe_sql)
            connection.set_progress_handler(
                state.observe_progress,
                trace.progress_handler_instructions,
            )
        except BaseException:
            _TRACE_CONNECTION_STATES.pop(key, None)
            raise
    else:
        state = prior
    state.refcount += 1
    state.add_trace(trace)
    return state


def _release_trace_connection(
    state: _TraceConnectionState,
    trace: SQLTrace,
    *,
    primary_error: BaseException | None,
) -> None:
    key = id(state.connection)
    current = _TRACE_CONNECTION_STATES.get(key)
    if current is not state:
        return
    state.refcount = max(0, state.refcount - 1)
    state.remove_trace(trace)
    if state.refcount:
        return
    _TRACE_CONNECTION_STATES.pop(key, None)
    cleanup_errors: list[BaseException] = []
    for operation in (
        lambda: state.connection.set_progress_handler(None, 0),
        lambda: state.connection.set_trace_callback(None),
    ):
        try:
            operation()
        except BaseException as cleanup_error:
            if _closed_connection_error(cleanup_error):
                continue
            cleanup_errors.append(cleanup_error)
    if cleanup_errors:
        if primary_error is None:
            raise cleanup_errors[0]
        for cleanup_error in cleanup_errors:
            primary_error.add_note(
                "benchmark connection callback cleanup failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )


def _push_progress_scope(
    connection: Any,
    bridge: Any,
    instructions: int,
) -> tuple[_TraceConnectionState | None, _ProgressRegistration | None]:
    state = _TRACE_CONNECTION_STATES.get(id(connection))
    if state is None or state.connection is not connection:
        return None, None
    if not bool(getattr(bridge, "enabled", True)):
        return state, None
    registration = _ProgressRegistration(bridge.sqlite_progress, instructions)
    state.progress_stack.append(registration)
    return state, registration


def _pop_progress_scope(
    state: _TraceConnectionState | None,
    registration: _ProgressRegistration | None,
    *,
    primary_error: BaseException | None,
) -> None:
    if state is None or registration is None:
        return
    if state.progress_stack and state.progress_stack[-1] is registration:
        state.progress_stack.pop()
    else:
        try:
            state.progress_stack.remove(registration)
        except ValueError:
            return
    _set_progress_safely(
        state,
        state.current_progress(),
        primary_error=primary_error,
    )


@contextlib.contextmanager
def _trace_connections(trace: SQLTrace) -> Iterator[None]:
    """Trace Semantic connections while preserving nested callback lifecycles."""

    components = _components()
    from neocortex.persistence.sqlite_cancellation import SQLiteCancellationBridge

    class CountingCancellationBridge(SQLiteCancellationBridge):
        __slots__ = ()

        def sqlite_progress(self) -> int:
            trace.observe_progress()
            return super().sqlite_progress()

    modules = (
        components.text_index,
        components.item_repository,
        components.generation_repository,
        components.generation_worker,
    )
    originals: list[tuple[Any, Any]] = []
    scope_originals: list[tuple[Any, Any]] = []
    had_bridge = hasattr(components.text_index, "SQLiteCancellationBridge")
    original_bridge = getattr(components.text_index, "SQLiteCancellationBridge", None)
    components.text_index.SQLiteCancellationBridge = CountingCancellationBridge
    for module in modules:
        original = getattr(module, "semantic_database", None)
        if original is None:
            continue
        originals.append((module, original))

        def counted_database(*args: object, _original=original, **kwargs: object):
            @contextlib.contextmanager
            def opened():
                connection: Any | None = None
                state: _TraceConnectionState | None = None
                primary_error: BaseException | None = None
                try:
                    with _original(*args, **kwargs) as opened_connection:
                        connection = opened_connection
                        state = _register_trace_connection(connection, trace)
                        try:
                            yield connection
                        except BaseException as error:
                            primary_error = error
                            raise
                except BaseException as error:
                    if primary_error is None:
                        primary_error = error
                    raise
                finally:
                    if state is not None:
                        _release_trace_connection(
                            state,
                            trace,
                            primary_error=primary_error,
                        )

            return opened()

        module.semantic_database = counted_database

        original_scope = getattr(module, "sqlite_cancellation_scope", None)
        if original_scope is not None:
            scope_originals.append((module, original_scope))

            def counted_scope(
                connection: Any,
                bridge: Any,
                *scope_args: object,
                _original_scope=original_scope,
                **scope_kwargs: object,
            ):
                instructions = int(
                    scope_kwargs.get(
                        "instructions",
                        scope_args[0] if scope_args else trace.progress_handler_instructions,
                    )
                )
                state, registration = _push_progress_scope(
                    connection,
                    bridge,
                    instructions,
                )
                primary_error: BaseException | None = None
                try:
                    with _original_scope(
                        connection,
                        bridge,
                        *scope_args,
                        **scope_kwargs,
                    ) as yielded:
                        yield yielded
                except BaseException as error:
                    primary_error = error
                    raise
                finally:
                    _pop_progress_scope(
                        state,
                        registration,
                        primary_error=primary_error,
                    )

            module.sqlite_cancellation_scope = contextlib.contextmanager(counted_scope)
    try:
        yield
    finally:
        for module, original in scope_originals:
            module.sqlite_cancellation_scope = original
        for module, original in originals:
            module.semantic_database = original
        if had_bridge:
            components.text_index.SQLiteCancellationBridge = original_bridge
        else:
            delattr(components.text_index, "SQLiteCancellationBridge")


def _summary_payload(summary: Any) -> dict[str, object]:
    return {
        "generation_id": int(summary.generation_id),
        "model_signature": str(summary.model_signature),
        "processing_signature": str(summary.processing_signature),
        "status": str(summary.status),
        "pending": int(summary.pending),
        "leased": int(summary.leased),
        "done": int(summary.done),
        "errors": int(summary.errors),
        "stale": int(summary.stale),
        "unfinished": int(summary.unfinished),
        "enumeration_complete": summary.cursor.get("enumeration_complete"),
    }


class BatchTrace:
    """Capture summary/stale state immediately after every completion/reuse page."""

    def __init__(self, components: LoadedComponents, sql_trace: SQLTrace) -> None:
        self.components = components
        self.sql_trace = sql_trace
        self.pages: list[dict[str, object]] = []

    def record(
        self,
        kind: str,
        database: Path,
        generation_id: int,
        *,
        sql_before: tuple[int, ...] | None = None,
        **details: object,
    ) -> None:
        selected_sql_before = (
            self.sql_trace.counters() if sql_before is None else sql_before
        )
        summary = self.components.generation_summary(
            database,
            generation_id,
            writer_coordinated=True,
        )
        self.pages.append(
            {
                "kind": kind,
                **details,
                "summary": _summary_payload(summary),
                "sql_delta": self.sql_trace.delta_since(selected_sql_before),
            }
        )


@contextlib.contextmanager
def _trace_generation_batches(trace: BatchTrace) -> Iterator[None]:
    components = _components()
    module = components.generation_worker
    original_complete = module.complete_embedding_jobs_batch
    original_reuse = module.reuse_cached_jobs

    def complete(database: Path, leases: Sequence[Any], successes: Sequence[Any], **kwargs: object):
        sql_before = trace.sql_trace.counters() if hasattr(trace, "sql_trace") else None
        started = time.perf_counter_ns()
        result = original_complete(database, leases, successes, **kwargs)
        elapsed_ns = time.perf_counter_ns() - started
        generation_id = int(leases[0].generation_id) if leases else 0
        if generation_id:
            trace.record(
                "complete",
                database,
                generation_id,
                lease_count=len(leases),
                success_count=len(successes),
                embedded=int(result[0]),
                failed=int(result[1]),
                elapsed_ns=elapsed_ns,
                sql_before=sql_before,
            )
        return result

    def reuse(database: Path, generation_id: int, *args: object, **kwargs: object):
        sql_before = trace.sql_trace.counters() if hasattr(trace, "sql_trace") else None
        started = time.perf_counter_ns()
        result = original_reuse(database, generation_id, *args, **kwargs)
        elapsed_ns = time.perf_counter_ns() - started
        if result:
            trace.record(
                "cache_hit",
                database,
                int(generation_id),
                reused=int(result),
                elapsed_ns=elapsed_ns,
                sql_before=sql_before,
            )
        return result

    module.complete_embedding_jobs_batch = complete
    module.reuse_cached_jobs = reuse
    try:
        yield
    finally:
        module.complete_embedding_jobs_batch = original_complete
        module.reuse_cached_jobs = original_reuse


def _model(components: LoadedComponents, label: str) -> Any:
    return components.EmbeddingModelSpec(
        model_signature=f"semantic-audit-deterministic-{label}",
        vector_space="semantic-audit-deterministic-space-v1",
        modality=components.EmbeddingModality.TEXT,
        model_id=f"fixture/semantic-audit-{label}",
        model_version="contract-fixture-v1",
        dimensions=4,
        provider="test-deterministic",
        supported_roles=(components.EmbeddingRole.QUERY, components.EmbeddingRole.PASSAGE),
        provenance={
            "audit_only": True,
            "backend_contract": FIXTURE_BACKEND_CONTRACT,
            "model_label": label,
        },
    )


def _chunking(components: LoadedComponents, *, changed: bool = False) -> Any:
    return components.TextChunkingConfig(
        max_chars=512,
        max_terms=96,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=64,
        algorithm_version=(
            "semantic-audit-natural-window-v2"
            if not changed
            else "semantic-audit-natural-window-v2-changed-contract"
        ),
        model_token_limit=512,
        tokenizer_signature="semantic-audit-deterministic-tokenizer-v1",
    )


def _body_text(index: int, *, changed: bool = False) -> str:
    suffix = " CAMBIO PEQUEÑO CONTROLADO." if changed else "."
    return (
        f"Transformador {index}; mantenimiento preventivo y pruebas del interruptor "
        f"de potencia, protección diferencial y aislamiento{suffix}"
    )


def _records(
    components: LoadedComponents,
    item_count: int,
    *,
    mutation: str = "base",
) -> tuple[Any, ...]:
    records: list[Any] = []
    for index in range(item_count):
        changed_item = mutation == "smallchange" and index == item_count - 1
        basename = f"informe-{index:07d}"
        if mutation == "move":
            path = f"C:/semantic-audit/moved/{basename}.pdf"
        elif mutation == "rename":
            path = f"C:/semantic-audit/{basename}-renamed.pdf"
        else:
            path = f"C:/semantic-audit/{basename}.pdf"
        text = _body_text(index, changed=changed_item)
        fingerprint = components.fingerprint_text(f"source:{index}:{text}")
        item = components.SemanticItem(
            item_id=f"item:pdf:semantic-audit-{index:07d}",
            source_kind=SOURCE_KIND,
            source_identity=f"semantic-audit-{index:07d}",
            identity_version="semantic-audit-source-v1",
            fingerprint=fingerprint,
            path=path,
            provenance={"benchmark": True, "fixture_version": "v1"},
            source_revision={
                "size": len(text.encode("utf-8")),
                "mtime_ns": 2 if mutation == "metadata" else 1,
                "birthtime_ns": 1,
                "processing_signature": "semantic-audit-source-v1",
            },
        )
        section = components.TextSection(
            "pdf_page",
            "1",
            text,
            {"benchmark": True, "page": 1},
        )
        records.append(components.TextSourceRecord(item, section))
    if mutation == "delete" and records:
        records.pop()
    return tuple(records)


def _fixture_digest(records: Sequence[Any]) -> str:
    """Digest the synthetic input contract without emitting fixture text."""

    digest = hashlib.sha256()
    for record in records:
        item = record.item
        section = record.section
        value = {
            "item_id": item.item_id,
            "source_kind": item.source_kind,
            "source_identity": item.source_identity,
            "identity_version": item.identity_version,
            "path": item.path,
            "fingerprint": {
                "xxh3_128": item.fingerprint.xxh3_128,
                "byte_count": item.fingerprint.byte_count,
                "xxh3_64_guard": item.fingerprint.xxh3_64_guard,
            },
            "provenance": item.provenance,
            "source_revision": item.source_revision,
            "section": {
                "kind": section.section_kind,
                "section_id": section.section_id,
                "text": section.text,
                "provenance": section.provenance,
            },
        }
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _generation_provenance(model: Any, chunking: Any) -> dict[str, object]:
    return {
        "benchmark_schema": BENCHMARK_SCHEMA,
        "backend_contract": FIXTURE_BACKEND_CONTRACT,
        "backend_batch_size": FIXTURE_BACKEND_BATCH_SIZE,
        "model_signature": model.model_signature,
        "sources": [SOURCE_KIND],
        "chunking_signature": chunking.signature,
        "fixture_version": "v1",
        "real_model": False,
    }


def _disk_bytes(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for label, candidate in (
        ("db", path),
        ("wal", Path(f"{path}-wal")),
        ("shm", Path(f"{path}-shm")),
        ("journal", Path(f"{path}-journal")),
    ):
        try:
            values[label] = int(candidate.stat().st_size)
        except FileNotFoundError:
            values[label] = 0
    values["total"] = sum(values.values())
    return values


def _state_snapshot(path: Path) -> dict[str, object]:
    components = _components()
    if not path.is_file():
        return {"pragmas": {}, "tables": {}, "dbstat_bytes": {}}
    tables: dict[str, int] = {}
    dbstat: dict[str, int] = {}
    pragmas: dict[str, object] = {}
    with components.semantic_schema.semantic_database(path, readonly=True) as connection:
        for name in (
            "page_count",
            "page_size",
            "freelist_count",
            "auto_vacuum",
            "schema_version",
            "data_version",
        ):
            pragmas[name] = int(connection.execute(f"PRAGMA {name}").fetchone()[0])
        pragmas["journal_mode"] = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        names = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        for name in names:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            tables[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        try:
            rows = connection.execute(
                "SELECT name,SUM(pgsize) FROM dbstat GROUP BY name ORDER BY name"
            )
        except sqlite3.DatabaseError:
            rows = ()
        dbstat = {str(row[0]): int(row[1]) for row in rows}
    return {"pragmas": pragmas, "tables": tables, "dbstat_bytes": dbstat}


def _quote_identifier(identifier: str) -> str:
    """Quote one harness-owned SQLite identifier without exposing it in output."""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise BenchmarkConfigurationError(f"invalid logical-integrity identifier: {identifier}")
    return f'"{identifier}"'


def _typed_cell(value: object) -> list[object]:
    """Return a type-tagged value for hashing, never for receipt emission."""

    if value is None:
        return ["null", None]
    if isinstance(value, bool):
        return ["bool", int(value)]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    raise TypeError(f"unsupported SQLite cell type: {type(value).__name__}")


def _typed_bytes(value: object) -> bytes:
    return json.dumps(
        _typed_cell(value),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_parts(parts: Iterator[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _table_logical_hash(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, object]:
    """Hash raw and logical typed rows in stable primary-key order."""

    quoted_table = _quote_identifier(table_name)
    table_info = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    columns = tuple(str(row[1]) for row in table_info)
    if not columns:
        raise BenchmarkConfigurationError(f"logical-integrity table has no columns: {table_name}")
    primary_key = tuple(
        str(row[1])
        for row in sorted(
            (row for row in table_info if int(row[5]) > 0),
            key=lambda row: int(row[5]),
        )
    )
    logical_columns = tuple(
        column
        for column in columns
        if column not in _VOLATILE_LOGICAL_COLUMNS
        and not column.endswith("_ns")
    )
    quoted_columns = ",".join(_quote_identifier(column) for column in columns)
    rows = connection.execute(
        f"SELECT {quoted_columns} FROM {quoted_table}"
    ).fetchall()
    column_index = {column: index for index, column in enumerate(columns)}
    raw_row_digests: list[tuple[bytes, str]] = []
    logical_row_digests: list[tuple[bytes, str]] = []
    primary_keys: list[bytes] = []
    null_primary_key_rows = 0
    for row in rows:
        key_values = tuple(row[column_index[column]] for column in primary_key)
        key_parts = tuple(_typed_bytes(value) for value in key_values)
        if any(value is None for value in key_values):
            null_primary_key_rows += 1
        key_bytes = b"".join(
            len(part).to_bytes(8, "big") + part for part in key_parts
        )
        raw_cell_parts = tuple(
            _typed_bytes(row[column_index[column]]) for column in columns
        )
        logical_cell_parts = tuple(
            _typed_bytes(row[column_index[column]]) for column in logical_columns
        )
        raw_row_digest = _digest_parts(
            iter(
                (
                    table_name.encode("utf-8"),
                    key_bytes,
                    *(
                        column.encode("utf-8") + b"\0" + cell
                        for column, cell in zip(columns, raw_cell_parts, strict=False)
                    ),
                )
            )
        )
        logical_row_digest = _digest_parts(
            iter(
                (
                    table_name.encode("utf-8"),
                    key_bytes,
                    *(
                        column.encode("utf-8") + b"\0" + cell
                        for column, cell in zip(logical_columns, logical_cell_parts, strict=False)
                    ),
                )
            )
        )
        # Tables without a declared PK still get deterministic ordering; the
        # logical row digest is only a local sort key and is never emitted.
        row_sort_key = (
            key_bytes
            if primary_key
            else logical_row_digest.encode("ascii")
        )
        raw_row_digests.append((row_sort_key, raw_row_digest))
        logical_row_digests.append((row_sort_key, logical_row_digest))
        primary_keys.append(key_bytes)
    raw_row_digests.sort(key=lambda item: item[0])
    logical_row_digests.sort(key=lambda item: item[0])
    ordered_raw_hashes = tuple(digest for _key, digest in raw_row_digests)
    ordered_logical_hashes = tuple(
        digest for _key, digest in logical_row_digests
    )
    raw_table_hash = _digest_parts(
        iter(
            (
                table_name.encode("utf-8"),
                json.dumps(columns, separators=(",", ":")).encode("utf-8"),
                *(digest.encode("ascii") for digest in ordered_raw_hashes),
            )
        )
    )
    logical_table_hash = _digest_parts(
        iter(
            (
                table_name.encode("utf-8"),
                json.dumps(logical_columns, separators=(",", ":")).encode("utf-8"),
                *(digest.encode("ascii") for digest in ordered_logical_hashes),
            )
        )
    )
    pk_cardinality = len(set(primary_keys)) if primary_key else len(rows)
    return {
        "row_count": len(rows),
        "stable_primary_key": list(primary_key),
        "stable_primary_key_cardinality": pk_cardinality,
        "null_primary_key_rows": null_primary_key_rows,
        "logical_columns_hashed": list(logical_columns),
        "volatile_columns_excluded": [
            column for column in columns if column not in logical_columns
        ],
        "raw_cells_hashed": len(rows) * len(columns),
        "cells_hashed": len(rows) * len(logical_columns),
        "raw_row_hash_cardinality": len(set(ordered_raw_hashes)),
        "row_hash_cardinality": len(set(ordered_logical_hashes)),
        "raw_hash": raw_table_hash,
        "logical_hash": logical_table_hash,
    }


def _orphan_checks(connection: sqlite3.Connection) -> dict[str, object]:
    """Run ten bounded relationship probes and emit counts only."""

    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    checks: list[dict[str, object]] = []
    for name, child, child_column, parent, parent_column in _ORPHAN_CHECKS:
        if child not in existing or parent not in existing:
            checks.append({"name": name, "status": "unavailable", "count": None})
            continue
        child_q = _quote_identifier(child)
        parent_q = _quote_identifier(parent)
        child_column_q = _quote_identifier(child_column)
        parent_column_q = _quote_identifier(parent_column)
        count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {child_q} AS child "
                f"LEFT JOIN {parent_q} AS parent "
                f"ON parent.{parent_column_q}=child.{child_column_q} "
                f"WHERE child.{child_column_q} IS NOT NULL "
                f"AND parent.{parent_column_q} IS NULL"
            ).fetchone()[0]
        )
        checks.append(
            {"name": name, "status": "pass" if count == 0 else "fail", "count": count}
        )
    available = all(check["status"] != "unavailable" for check in checks)
    all_zero = available and all(check["count"] == 0 for check in checks)
    return {
        "check_count": len(checks),
        "available": available,
        "all_zero": all_zero,
        "checks": checks,
    }


def _logical_integrity_snapshot_connection(
    connection: sqlite3.Connection,
) -> dict[str, object]:
    """Capture structure, typed-cell digests and orphan counts only."""

    table_names = tuple(
        sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
    )
    tables = {name: _table_logical_hash(connection, name) for name in table_names}
    raw_aggregate_hash = _digest_parts(
        iter(
            name.encode("utf-8")
            + b"\0"
            + str(tables[name]["raw_hash"]).encode("ascii")
            for name in table_names
        )
    )
    logical_aggregate_hash = _digest_parts(
        iter(
            name.encode("utf-8")
            + b"\0"
            + str(tables[name]["logical_hash"]).encode("ascii")
            for name in table_names
        )
    )
    return {
        "schema": LOGICAL_INTEGRITY_SCHEMA,
        "hash_algorithm": "sha256",
        "serialization": "typed-tagged-cells; rows ordered by stable primary-key bytes",
        "source_values_emitted": False,
        "sql_literals_emitted": False,
        "raw_aggregate_hash": raw_aggregate_hash,
        "logical_aggregate_hash": logical_aggregate_hash,
        "table_count": len(table_names),
        "tables": tables,
        "orphan_checks": _orphan_checks(connection),
    }


def _logical_integrity_snapshot(path: Path) -> dict[str, object]:
    components = _components()
    with components.semantic_schema.semantic_database(path, readonly=True) as connection:
        return _logical_integrity_snapshot_connection(connection)


def _delta(before: Mapping[str, object], after: Mapping[str, object]) -> dict[str, int]:
    result: dict[str, int] = {}
    for key in set(before) | set(after):
        old = before.get(key, 0)
        new = after.get(key, 0)
        if isinstance(old, int) and isinstance(new, int):
            result[key] = new - old
    return result


def _resource_snapshot() -> dict[str, int]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # Linux reports ru_maxrss in KiB.  Keep the unit explicit for portability.
    return {
        "user_cpu_ns": int(usage.ru_utime * 1_000_000_000),
        "system_cpu_ns": int(usage.ru_stime * 1_000_000_000),
        "max_rss_kib": int(usage.ru_maxrss),
    }


_PROCESS_IO_KEYS = (
    "rchar",
    "wchar",
    "read_bytes",
    "write_bytes",
    "cancelled_write_bytes",
)


def _process_io_snapshot() -> dict[str, int] | None:
    """Read Linux process-I/O counters without touching the SQLite owner."""

    try:
        lines = Path("/proc/self/io").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    values: dict[str, int] = {}
    for line in lines:
        name, separator, raw_value = line.partition(":")
        if not separator or name not in _PROCESS_IO_KEYS:
            continue
        try:
            values[name] = int(raw_value.strip())
        except ValueError:
            return None
    return values if all(key in values for key in _PROCESS_IO_KEYS) else None


@contextlib.contextmanager
def _phase_metrics(
    database: Path,
    *,
    trace_python_memory: bool,
    capture_process_io: bool = False,
) -> Iterator[dict[str, object]]:
    before_disk = _disk_bytes(database)
    before_state = _state_snapshot(database)
    before_resource = _resource_snapshot()
    # Capture immediately before the productive operation, after the
    # diagnostic pre-snapshot.  The matching after sample is taken before the
    # diagnostic post-snapshot, so process I/O does not include logical hashes
    # or orphan probes.
    before_process_io = _process_io_snapshot() if capture_process_io else None
    if trace_python_memory:
        tracemalloc.start()
    started = time.perf_counter_ns()
    try:
        yield_context = {}
        yield yield_context
    finally:
        elapsed_ns = time.perf_counter_ns() - started
        if trace_python_memory:
            _current, peak_heap = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        else:
            peak_heap = None
        after_process_io = _process_io_snapshot() if capture_process_io else None
        after_disk = _disk_bytes(database)
        after_state = _state_snapshot(database)
        after_resource = _resource_snapshot()
        yield_context.update(
            {
                "elapsed_ns": elapsed_ns,
                "wall_seconds": elapsed_ns / 1_000_000_000,
                "cpu_user_ns": after_resource["user_cpu_ns"] - before_resource["user_cpu_ns"],
                "cpu_system_ns": after_resource["system_cpu_ns"] - before_resource["system_cpu_ns"],
                "cpu_seconds": (
                    after_resource["user_cpu_ns"]
                    - before_resource["user_cpu_ns"]
                    + after_resource["system_cpu_ns"]
                    - before_resource["system_cpu_ns"]
                ) / 1_000_000_000,
                "max_rss_kib": after_resource["max_rss_kib"],
                "max_rss_bytes": after_resource["max_rss_kib"] * 1024,
                "max_rss_mib": after_resource["max_rss_kib"] / 1024,
                "peak_python_heap_bytes": (
                    None if peak_heap is None else int(peak_heap)
                ),
                "python_memory_trace": trace_python_memory,
                "disk_before": before_disk,
                "disk_after": after_disk,
                "disk_delta": _delta(before_disk, after_disk),
                "state_before": before_state,
                "state_after": after_state,
                "table_delta": _delta(before_state["tables"], after_state["tables"]),
                "dbstat_delta": _delta(before_state["dbstat_bytes"], after_state["dbstat_bytes"]),
                "process_io": (
                    None
                    if before_process_io is None or after_process_io is None
                    else {
                        "before": before_process_io,
                        "after": after_process_io,
                        "delta": {
                            key: after_process_io[key] - before_process_io[key]
                            for key in _PROCESS_IO_KEYS
                        },
                    }
                ),
                "process_io_status": (
                    "measured"
                    if before_process_io is not None and after_process_io is not None
                    else "unavailable"
                ),
                # State snapshots and typed logical hashes are deliberately
                # taken after the performance window.  Their counters never
                # enter wall/CPU/SQL/VM deltas above.
                "diagnostics_outside_performance_window": True,
            }
        )


def _expose_phase_metrics(metrics: dict[str, object]) -> None:
    """Expose the measured operation counters without duplicating diagnostics."""

    operation = metrics.get("operation")
    if not isinstance(operation, Mapping):
        return
    rates = operation.get("rates")
    if isinstance(rates, Mapping):
        for name in (
            "jobs_per_second",
            "backend_requests_per_second",
            "new_jobs_admitted_per_second",
            "staged_jobs_per_second",
        ):
            value = rates.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[name] = float(value)
    sql = operation.get("sql")
    if isinstance(sql, Mapping):
        for source, target in (
            ("total_statements", "sql_total"),
            ("write_statements", "sql_writes"),
            ("commits", "sql_commits"),
            ("transactions", "sql_transactions"),
            (
                "sqlite_progress_vm_instructions_estimate",
                "sqlite_vm_instructions_estimate",
            ),
        ):
            value = sql.get(source)
            if isinstance(value, int) and not isinstance(value, bool):
                metrics[target] = value


def _run_operation(
    database: Path,
    *,
    records: Sequence[Any],
    model: Any,
    chunking: Any,
    processing_signature: str,
    components: LoadedComponents,
    point_seconds: float,
    base_generation_id: int | None,
    initialize: bool,
) -> dict[str, object]:
    """Run one bounded stage/generation operation and return observable facts."""

    backend = CountingDeterministicBackend(components, model)
    phase_trace = SQLTrace()
    batch_trace = BatchTrace(components, phase_trace)
    budget = components.SemanticWorkBudget.from_time_budget(
        max_items=max(1, len(records) + 1),
        max_new_jobs=max(64, len(records) * 4 + 64),
        time_budget_seconds=point_seconds,
    )
    stage_result: tuple[int, int, int, bool] | None = None
    generation_result: Any | None = None
    preparation_result: Any | None = None
    generation_id = 0
    summary_override: Any | None = None
    error: dict[str, str] | None = None
    interrupted = False
    started = time.perf_counter_ns()
    try:
        with _trace_connections(phase_trace), _trace_generation_batches(batch_trace):
            if initialize:
                components.initialize_semantic_state(database)
            # Registration is idempotent for the base model and is required
            # for the explicitly labelled deterministic model-change point.
            components.register_embedding_model(database, model, allow_test_provider=True)
            provenance = _generation_provenance(model, chunking)
            generation_id = components.start_embedding_generation(
                database,
                model_signature=model.model_signature,
                processing_signature=processing_signature,
                provenance=provenance,
                cursor={"protocol": "semantic-audit-v1", "enumeration_complete": False},
                materialize_base=False,
                work_budget=budget,
            )
            stage_result = components.text_index._stage_source(
                database,
                database.parent,
                SOURCE_KIND,
                generation_id=generation_id,
                refresh_token=f"generation:{generation_id}:source:{SOURCE_KIND}",
                chunking=chunking,
                token_counter=backend.text_token_counts,
                source_record_iterator=(
                    lambda _state, _source: iter(tuple(records))
                ),
                base_generation_id=base_generation_id,
                work_budget=budget,
            )
            source_complete = bool(stage_result[3])
            if source_complete:
                preparation_result = components.prepare_embedding_generation(
                    database,
                    generation_id,
                    enumeration_complete=True,
                    work_budget=budget,
                )
                if preparation_result is None:
                    generation_result = components.run_generation(
                        database,
                        generation_id,
                        backend,
                        queued=int(stage_result[2]),
                        work_budget=budget,
                        publish_if_complete=True,
                    )
                else:
                    # Exact replay deletes the empty candidate and returns
                    # the unchanged published-head summary.
                    summary_override = preparation_result
    except BaseException as exc:
        # Deadline exhaustion is the only expected non-terminal result.  Keep
        # KeyboardInterrupt visible so the coordinator can stop the harness.
        if isinstance(exc, TimeoutError):
            interrupted = True
            error = {"type": type(exc).__name__, "message": str(exc)[:512]}
        else:
            raise
    elapsed_ns = time.perf_counter_ns() - started
    try:
        summary = summary_override or components.generation_summary(
            database,
            generation_id,
            writer_coordinated=True,
        )
        summary_payload = _summary_payload(summary)
    except (OSError, sqlite3.DatabaseError, KeyError, ValueError) as exc:
        summary_payload = {"error": f"{type(exc).__name__}: {exc}"}
    status = (
        "ready"
        if isinstance(summary_payload, dict)
        and summary_payload.get("status") == "ready"
        and summary_payload.get("unfinished") == 0
        and summary_payload.get("errors") == 0
        and summary_payload.get("stale") == 0
        else "partial"
    )
    if interrupted:
        status = "partial"
    reported_generation_id = int(summary_payload.get("generation_id", generation_id))
    elapsed_seconds = elapsed_ns / 1_000_000_000
    generation_payload = (
        None
        if generation_result is None
        else {
            "queued": int(generation_result.queued),
            "reused": int(generation_result.reused),
            "embedded": int(generation_result.embedded),
            "failed": int(generation_result.failed),
            "stop_reason": generation_result.stop_reason,
        }
    )
    processed_jobs = (
        0
        if generation_payload is None
        else sum(
            int(generation_payload[key])
            for key in ("reused", "embedded", "failed")
        )
    )
    admitted_jobs = int(budget.new_jobs_admitted)
    stage_jobs = 0 if stage_result is None else int(stage_result[2])
    return {
        "status": status,
        "generation_id": reported_generation_id,
        "stage": None if stage_result is None else {
            "items_staged": int(stage_result[0]),
            "chunks_staged": int(stage_result[1]),
            "queued": int(stage_result[2]),
            "source_complete": bool(stage_result[3]),
            "new_jobs_admitted": int(budget.new_jobs_admitted),
            "rebound_members": int(budget.rebound_members),
            "truncation_reason": budget.truncation_reason,
        },
        "preparation": (
            None
            if preparation_result is None
            else (
                "exact_replay"
                if generation_result is None
                and getattr(preparation_result, "generation_id", None) is not None
                else "materialized"
            )
        ),
        "generation": generation_payload,
        "summary": summary_payload,
        "batch_pages": batch_trace.pages,
        "backend": {
            "calls": backend.calls,
            "requests": backend.requests,
            "input_bytes": backend.input_bytes,
            "batch_sizes": backend.batch_sizes,
            "token_count_calls": backend.token_count_calls,
            "token_count_inputs": backend.token_count_inputs,
            "elapsed_ns": backend.elapsed_ns,
        },
        "elapsed_ns": elapsed_ns,
        "wall_seconds": elapsed_seconds,
        "rates": {
            # ``jobs_per_second`` is durable worker work (embedded, reused or
            # failed).  Backend request rate is kept separate so exact replay
            # cannot be mistaken for model throughput.
            "jobs_per_second": processed_jobs / elapsed_seconds,
            "backend_requests_per_second": backend.requests / elapsed_seconds,
            "new_jobs_admitted_per_second": admitted_jobs / elapsed_seconds,
            "staged_jobs_per_second": stage_jobs / elapsed_seconds,
            "processed_jobs": processed_jobs,
            "backend_requests": backend.requests,
            "new_jobs_admitted": admitted_jobs,
            "staged_jobs": stage_jobs,
        },
        "sql": phase_trace.as_dict(),
        "error": error,
        "budget": {
            "items_admitted": int(budget.items_admitted),
            "new_jobs_admitted": int(budget.new_jobs_admitted),
            "rebound_members": int(budget.rebound_members),
            "truncated": budget.truncated,
            "truncation_reason": budget.truncation_reason,
        },
    }


def _run_point(
    *,
    components: LoadedComponents,
    target_jobs: int,
    case: str,
    point_seconds: float,
    trace_python_memory: bool,
    logical_hashes: bool,
    capture_process_io: bool,
    snapshot_dir: Path | None,
    work_root: Path,
) -> dict[str, object]:
    if target_jobs < 2 or target_jobs % 2:
        raise BenchmarkConfigurationError(
            "target jobs must be an even positive count so the fixture has two chunks per item"
        )
    item_count = target_jobs // 2
    database = work_root / "semantic.sqlite3"
    base_model = _model(components, "v1")
    base_chunking = _chunking(components)
    changed_chunking = _chunking(components, changed=True)
    successor_model = _model(components, "v2") if case == "model_signature_change" else base_model
    successor_chunking = changed_chunking if case == "chunking_signature_change" else base_chunking
    mutation = {
        "identical": "base",
        "metadata": "metadata",
        "smallchange": "smallchange",
        "move": "move",
        "rename": "rename",
        "delete": "delete",
        "model_signature_change": "base",
        "chunking_signature_change": "base",
    }[case]
    base_records = _records(components, item_count)
    successor_records = _records(components, item_count, mutation=mutation)
    fixture_digest = _fixture_digest(base_records)
    base_processing = (
        f"{BASE_PROCESSING_SIGNATURE}|model={base_model.model_signature}|"
        f"chunking={base_chunking.signature}"
    )
    successor_processing = (
        f"{BASE_PROCESSING_SIGNATURE}|model={successor_model.model_signature}|"
        f"chunking={successor_chunking.signature}"
    )
    started = _datetime.datetime.now(_datetime.UTC).isoformat()
    phases: dict[str, object] = {}
    snapshots: dict[str, dict[str, object]] = {}

    with _phase_metrics(
        database,
        trace_python_memory=trace_python_memory,
        capture_process_io=capture_process_io,
    ) as baseline_metrics:
        # _run_operation creates the database and all Semantic state in the
        # assigned temporary directory.  No configured owner is consulted.
        baseline = _run_operation(
            database,
            records=base_records,
            model=base_model,
            chunking=base_chunking,
            processing_signature=base_processing,
            components=components,
            point_seconds=point_seconds,
            base_generation_id=None,
            initialize=True,
        )
        baseline_metrics["operation"] = baseline
        _expose_phase_metrics(baseline_metrics)
    if logical_hashes:
        baseline_metrics["logical_integrity"] = _logical_integrity_snapshot(database)
    if snapshot_dir is not None:
        snapshots["baseline"] = _snapshot_phase(
            database,
            fixture_root=work_root,
            snapshot_dir=snapshot_dir,
            case=case,
            target_jobs=target_jobs,
            phase="baseline",
        )
        baseline_metrics["snapshot"] = snapshots["baseline"]
    phases["baseline"] = baseline_metrics

    baseline_ready = baseline.get("status") == "ready"
    if not baseline_ready:
        if snapshot_dir is not None:
            snapshots["successor"] = _snapshot_skipped(
                snapshot_dir,
                case=case,
                target_jobs=target_jobs,
                phase="successor",
                reason="baseline_nonterminal",
                database=database,
            )
            snapshots["replay"] = _snapshot_skipped(
                snapshot_dir,
                case=case,
                target_jobs=target_jobs,
                phase="replay",
                reason="baseline_nonterminal",
                database=database,
            )
        return {
            "schema": BENCHMARK_SCHEMA,
            "started_utc": started,
            "case": case,
            "target_jobs": target_jobs,
            "item_count": item_count,
            "fixture_digest_sha256": fixture_digest,
            "model_real": False,
            "status": "partial",
            "reason": "baseline_deadline_or_nonterminal",
            "phases": phases,
            **({"snapshots": snapshots} if snapshot_dir is not None else {}),
        }

    baseline_generation_id = int(baseline["generation_id"])
    with _phase_metrics(
        database,
        trace_python_memory=trace_python_memory,
        capture_process_io=capture_process_io,
    ) as successor_metrics:
        successor = _run_operation(
            database,
            records=successor_records,
            model=successor_model,
            chunking=successor_chunking,
            processing_signature=successor_processing,
            components=components,
            point_seconds=point_seconds,
            # A changed chunking contract must enumerate every item.  The
            # production text facade deliberately drops its published delta
            # in this case instead of treating the old chunks as unchanged.
            base_generation_id=(
                None
                if case in {"model_signature_change", "chunking_signature_change"}
                else baseline_generation_id
            ),
            initialize=False,
        )
        successor_metrics["operation"] = successor
        _expose_phase_metrics(successor_metrics)
    if logical_hashes:
        successor_metrics["logical_integrity"] = _logical_integrity_snapshot(database)
    if snapshot_dir is not None:
        snapshots["successor"] = _snapshot_phase(
            database,
            fixture_root=work_root,
            snapshot_dir=snapshot_dir,
            case=case,
            target_jobs=target_jobs,
            phase="successor",
        )
        successor_metrics["snapshot"] = snapshots["successor"]
    phases["successor"] = successor_metrics

    successor_ready = successor.get("status") == "ready"
    if successor_ready:
        with _phase_metrics(
            database,
            trace_python_memory=trace_python_memory,
            capture_process_io=capture_process_io,
        ) as replay_metrics:
            replay = _run_operation(
                database,
                records=successor_records,
                model=successor_model,
                chunking=successor_chunking,
                processing_signature=successor_processing,
                components=components,
                point_seconds=point_seconds,
                base_generation_id=int(successor["generation_id"]),
                initialize=False,
            )
            replay_metrics["operation"] = replay
            _expose_phase_metrics(replay_metrics)
        if logical_hashes:
            replay_metrics["logical_integrity"] = _logical_integrity_snapshot(database)
        if snapshot_dir is not None:
            snapshots["replay"] = _snapshot_phase(
                database,
                fixture_root=work_root,
                snapshot_dir=snapshot_dir,
                case=case,
                target_jobs=target_jobs,
                phase="replay",
            )
            replay_metrics["snapshot"] = snapshots["replay"]
        phases["replay"] = replay_metrics
    else:
        phases["replay"] = {"operation": {"status": "skipped", "reason": "successor_nonterminal"}}
        if snapshot_dir is not None:
            snapshots["replay"] = _snapshot_skipped(
                snapshot_dir,
                case=case,
                target_jobs=target_jobs,
                phase="replay",
                reason="successor_nonterminal",
                database=database,
            )
            phases["replay"]["snapshot"] = snapshots["replay"]

    replay = phases["replay"].get("operation", {}) if isinstance(phases["replay"], dict) else {}
    replay_ready = isinstance(replay, dict) and replay.get("status") == "ready"
    result = {
        "schema": BENCHMARK_SCHEMA,
        "started_utc": started,
        "case": case,
        "target_jobs": target_jobs,
        "item_count": item_count,
        "fixture_digest_sha256": fixture_digest,
        "fixture_contract": {
            "source_kind": SOURCE_KIND,
            "items": item_count,
            "chunks": target_jobs,
            "two_chunks_per_item": True,
            "jobs": target_jobs,
            "backend": FIXTURE_BACKEND_CONTRACT,
            "backend_batch_size": FIXTURE_BACKEND_BATCH_SIZE,
            "model_signature": base_model.model_signature,
            "chunking_signature": base_chunking.signature,
            "lifecycle_order": list(LIFECYCLE_ORDER),
        },
        "model_real": False,
        "hashes_outside_performance_window": bool(logical_hashes),
        "process_io_capture": bool(capture_process_io),
        "status": "complete" if successor_ready and replay_ready else "partial",
        "phases": phases,
        "assertions": {
            "baseline_ready": baseline_ready,
            "successor_ready": successor_ready,
            "replay_ready": replay_ready,
            "replay_no_backend_calls": (
                isinstance(replay, dict)
                and int(replay.get("backend", {}).get("calls", -1)) == 0
                if isinstance(replay, dict)
                else False
            ),
            "replay_idempotent_generation_or_head": (
                isinstance(replay, dict)
                and replay.get("preparation") == "exact_replay"
                if isinstance(replay, dict)
                else False
            ),
        },
    }
    if logical_hashes:
        successor_integrity = phases["successor"].get("logical_integrity", {})
        replay_integrity = phases["replay"].get("logical_integrity", {})
        successor_logical_hash = (
            successor_integrity.get("logical_aggregate_hash")
            if isinstance(successor_integrity, dict)
            else None
        )
        replay_logical_hash = (
            replay_integrity.get("logical_aggregate_hash")
            if isinstance(replay_integrity, dict)
            else None
        )
        successor_raw_hash = (
            successor_integrity.get("raw_aggregate_hash")
            if isinstance(successor_integrity, dict)
            else None
        )
        replay_raw_hash = (
            replay_integrity.get("raw_aggregate_hash")
            if isinstance(replay_integrity, dict)
            else None
        )
        raw_diff_tables = (
            sorted(
                name
                for name in successor_integrity.get("tables", {})
                if successor_integrity["tables"][name].get("raw_hash")
                != replay_integrity.get("tables", {}).get(name, {}).get("raw_hash")
            )
            if isinstance(successor_integrity, dict)
            and isinstance(replay_integrity, dict)
            else []
        )
        logical_diff_tables = (
            sorted(
                name
                for name in successor_integrity.get("tables", {})
                if successor_integrity["tables"][name].get("logical_hash")
                != replay_integrity.get("tables", {}).get(name, {}).get("logical_hash")
            )
            if isinstance(successor_integrity, dict)
            and isinstance(replay_integrity, dict)
            else []
        )
        result["logical_hashes"] = True
        result["assertions"].update(
            {
                "replay_logical_hash_equal": (
                    bool(successor_logical_hash)
                    and successor_logical_hash == replay_logical_hash
                ),
                "replay_raw_hash_equal": (
                    bool(successor_raw_hash)
                    and successor_raw_hash == replay_raw_hash
                ),
                "replay_raw_hash_diff_tables": raw_diff_tables,
                "replay_logical_hash_diff_tables": logical_diff_tables,
                "orphan_checks_zero": (
                    isinstance(successor_integrity, dict)
                    and isinstance(replay_integrity, dict)
                    and successor_integrity.get("orphan_checks", {}).get("all_zero")
                    and replay_integrity.get("orphan_checks", {}).get("all_zero")
                ),
            }
        )
    if snapshot_dir is not None:
        result["snapshots"] = snapshots
    return result


_REQUIRED_POINT_ASSERTIONS = (
    "baseline_ready",
    "successor_ready",
    "replay_ready",
    "replay_no_backend_calls",
    "replay_idempotent_generation_or_head",
)


def _point_failure_reason(
    result: Mapping[str, object],
    *,
    logical_hashes: bool,
) -> str | None:
    """Return a terminal gate failure without changing the structured row."""

    status = result.get("status")
    if status != "complete":
        return f"status={status!r}"
    raw_assertions = result.get("assertions")
    if not isinstance(raw_assertions, Mapping):
        return "assertions_missing"
    required = list(_REQUIRED_POINT_ASSERTIONS)
    if logical_hashes:
        required.extend(("replay_logical_hash_equal", "orphan_checks_zero"))
    for name in required:
        if raw_assertions.get(name) is not True:
            return f"required_assertion_false:{name}"
    return None


def _parse_sizes(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise BenchmarkConfigurationError("--sizes must be comma-separated positive integers") from exc
    if not values or any(value not in SUPPORTED_SIZES for value in values):
        raise BenchmarkConfigurationError(
            f"--sizes must use the bounded set {','.join(map(str, SUPPORTED_SIZES))}"
        )
    if len(set(values)) != len(values):
        raise BenchmarkConfigurationError("--sizes cannot repeat a point")
    return values


def _parse_point_seconds(raw: float) -> float:
    if not isinstance(raw, (int, float)) or not 0.001 <= float(raw) <= MAX_POINT_SECONDS:
        raise BenchmarkConfigurationError(
            f"--point-time-budget must be between 0.001 and {MAX_POINT_SECONDS:g} seconds"
        )
    return float(raw)


def _parse_group_seconds(raw: float) -> float:
    if not isinstance(raw, (int, float)) or not 0.001 <= float(raw) <= MAX_GROUP_SECONDS:
        raise BenchmarkConfigurationError(
            f"--group-time-budget must be between 0.001 and {MAX_GROUP_SECONDS:g} seconds"
        )
    return float(raw)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_product_path(path: Path, *, label: str, repository_root: Path) -> None:
    """Reject checkout and well-known installed-state roots for benchmark data."""

    repository = repository_root.expanduser().resolve()
    if _is_within(path, repository) or _is_within(repository, path):
        raise BenchmarkConfigurationError(
            f"{label} must not be the checkout or contain the checkout: {path}"
        )
    home = Path.home().resolve()
    product_roots = (
        home / ".config" / "Neocortex",
        home / ".local" / "share" / "Neocortex",
        home / ".local" / "state" / "Neocortex",
        home / ".cache" / "Neocortex",
    )
    if any(_is_within(path, root) or _is_within(root, path) for root in product_roots):
        raise BenchmarkConfigurationError(
            f"{label} must not overlap an installed NeoCortex state root: {path}"
        )


def _effective_work_root(path: Path | None, *, repository_root: Path) -> tuple[Path, bool]:
    """Return a private parent and whether this invocation owns its cleanup."""

    if path is None:
        return (
            Path(tempfile.mkdtemp(prefix="neocortex-generation-control-", dir=SYSTEM_TEMP_ROOT)),
            True,
        )
    if not path.is_absolute():
        raise BenchmarkConfigurationError("--temp-root must be an absolute path")
    selected = path.expanduser().resolve()
    _reject_product_path(selected, label="--temp-root", repository_root=repository_root)
    selected.mkdir(parents=True, exist_ok=True, mode=0o700)
    return selected, False


def _private_environment(root: Path) -> None:
    """Bind HOME/XDG/model caches to the isolated run root before imports."""

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
    environment = {
        name: str(directory) for name, directory in directories.items()
    }
    environment.update(
        {
            "HF_HUB_CACHE": str(root / "model-cache" / "huggingface" / "hub"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "NEOCORTEX_PROGRESS_STREAM": "0",
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PIP_NO_INDEX": "1",
            "DO_NOT_TRACK": "1",
            "ORT_DISABLE_TELEMETRY": "1",
        }
    )
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


def _file_fence(path: Path) -> dict[str, object]:
    """Return a non-content fence for one source/destination path."""

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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _abstained_snapshot(
    *,
    case: str,
    target_jobs: int,
    phase: str,
    reason: str,
    database: Path,
    files: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "case": case,
        "target_jobs": target_jobs,
        "phase": phase,
        "status": "abstained",
        "reason": reason,
        "source_path": str(database),
        "destination_path": None,
        "files": {} if files is None else dict(files),
    }


def _append_snapshot_manifest(snapshot_dir: Path, entry: Mapping[str, object]) -> None:
    manifest_path = snapshot_dir / "snapshot-manifest.jsonl"
    with manifest_path.open("a", encoding="utf-8") as stream:
        _write_jsonl(stream, entry)


def _copy_fenced_file(source: Path, destination: Path) -> dict[str, object]:
    """Copy one regular file with before/after source and byte fences."""

    before = _file_fence(source)
    if before.get("status") != "present":
        return {
            "status": "abstained",
            "reason": f"source_{before.get('status')}",
            "source_path": str(source),
            "destination_path": None,
            "source_fence_before": before,
            "source_fence_after": _file_fence(source),
        }
    destination_before = _file_fence(destination)
    if destination_before.get("status") != "absent":
        return {
            "status": "abstained",
            "reason": "destination_collision",
            "source_path": str(source),
            "destination_path": str(destination),
            "source_fence_before": before,
            "source_fence_after": _file_fence(source),
            "destination_fence_before": destination_before,
        }
    destination_created = False
    try:
        with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
            destination_created = True
            os.fchmod(destination_stream.fileno(), 0o600)
            for block in iter(lambda: source_stream.read(1024 * 1024), b""):
                destination_stream.write(block)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
    except (OSError, ValueError) as exc:
        if destination_created:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        return {
            "status": "abstained",
            "reason": f"copy_error:{type(exc).__name__}",
            "source_path": str(source),
            "destination_path": str(destination),
            "source_fence_before": before,
            "source_fence_after": _file_fence(source),
            "destination_fence_after": _file_fence(destination),
        }
    after = _file_fence(source)
    destination_after = _file_fence(destination)
    try:
        source_hash = _file_sha256(source)
        destination_hash = _file_sha256(destination)
    except OSError:
        source_hash = destination_hash = None
    after_hash = _file_fence(source)
    stable = before == after == after_hash
    byte_equal = (
        stable
        and destination_after.get("status") == "present"
        and source_hash is not None
        and source_hash == destination_hash
    )
    if not byte_equal:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        return {
            "status": "abstained",
            "reason": "source_changed_or_byte_mismatch",
            "source_path": str(source),
            "destination_path": str(destination),
            "source_fence_before": before,
            "source_fence_after_copy": after,
            "source_fence_after": after_hash,
            "destination_fence_after": destination_after,
        }
    return {
        "status": "copied",
        "source_path": str(source),
        "destination_path": str(destination),
        "size": int(before["size"]),
        "sha256": destination_hash,
        "source_fence_before": before,
        "source_fence_after_copy": after,
        "source_fence_after": after_hash,
        "destination_fence_after": destination_after,
    }


def _snapshot_phase(
    database: Path,
    *,
    fixture_root: Path,
    snapshot_dir: Path,
    case: str,
    target_jobs: int,
    phase: str,
) -> dict[str, object]:
    """Retain one closed temporary owner without checkpointing sidecars."""

    if not _is_within(database.resolve(), fixture_root.resolve()):
        entry = _abstained_snapshot(
            case=case,
            target_jobs=target_jobs,
            phase=phase,
            reason="source_outside_fixture_root",
            database=database,
        )
        _append_snapshot_manifest(snapshot_dir, entry)
        return entry
    phase_dir = snapshot_dir / f"{case}-{target_jobs}" / phase
    phase_dir_parent = phase_dir.parent
    if phase_dir_parent.exists() and not phase_dir_parent.is_dir():
        entry = _abstained_snapshot(
            case=case,
            target_jobs=target_jobs,
            phase=phase,
            reason="destination_parent_collision",
            database=database,
        )
        _append_snapshot_manifest(snapshot_dir, entry)
        return entry
    if phase_dir.exists() or phase_dir.is_symlink():
        entry = _abstained_snapshot(
            case=case,
            target_jobs=target_jobs,
            phase=phase,
            reason="destination_collision",
            database=database,
        )
        _append_snapshot_manifest(snapshot_dir, entry)
        return entry
    phase_dir.mkdir(parents=True, mode=0o700)
    source_fences = {
        "database": _file_fence(database),
        **{
            suffix.lstrip("-"): _file_fence(Path(f"{database}{suffix}"))
            for suffix in SNAPSHOT_SIDECARS
        },
    }
    sidecar_data = {
        name: fence
        for name, fence in source_fences.items()
        if name != "database"
        and fence.get("status") == "present"
        and int(fence.get("size", 0)) > 0
    }
    sidecar_invalid = {
        name: fence
        for name, fence in source_fences.items()
        if name != "database"
        and fence.get("status") not in {"absent", "present"}
    }
    if source_fences["database"].get("status") != "present":
        entry = _abstained_snapshot(
            case=case,
            target_jobs=target_jobs,
            phase=phase,
            reason="database_not_regular",
            database=database,
            files=source_fences,
        )
        _append_snapshot_manifest(snapshot_dir, entry)
        return entry
    if sidecar_data:
        entry = _abstained_snapshot(
            case=case,
            target_jobs=target_jobs,
            phase=phase,
            reason="nonempty_sidecar_data_no_checkpoint",
            database=database,
            files=source_fences,
        )
        _append_snapshot_manifest(snapshot_dir, entry)
        return entry
    if sidecar_invalid:
        entry = _abstained_snapshot(
            case=case,
            target_jobs=target_jobs,
            phase=phase,
            reason="sidecar_not_regular",
            database=database,
            files=source_fences,
        )
        _append_snapshot_manifest(snapshot_dir, entry)
        return entry

    database_copy = phase_dir / database.name
    database_entry = _copy_fenced_file(database, database_copy)
    source_fences_after = {
        "database": _file_fence(database),
        **{
            suffix.lstrip("-"): _file_fence(Path(f"{database}{suffix}"))
            for suffix in SNAPSHOT_SIDECARS
        },
    }
    sidecar_changed = source_fences_after != source_fences
    if sidecar_changed or database_entry.get("status") != "copied":
        entry = _abstained_snapshot(
            case=case,
            target_jobs=target_jobs,
            phase=phase,
            reason=(
                "source_or_sidecar_changed_after_copy"
                if sidecar_changed
                else str(database_entry.get("reason", "database_copy_abstained"))
            ),
            database=database,
            files={
                "database": database_entry,
                "sidecars_before": source_fences,
                "sidecars_after": source_fences_after,
            },
        )
        _append_snapshot_manifest(snapshot_dir, entry)
        return entry

    entry = {
        "schema": SNAPSHOT_SCHEMA,
        "case": case,
        "target_jobs": target_jobs,
        "phase": phase,
        "status": "copied",
        "source_path": str(database),
        "destination_path": str(database_copy),
        "files": {
            "database": database_entry,
            "wal": source_fences["wal"],
            "shm": source_fences["shm"],
            "journal": source_fences["journal"],
        },
        "source_sidecars_before": source_fences,
        "source_sidecars_after": source_fences_after,
        "sidecars_empty_or_absent": True,
        "checkpoint_performed": False,
    }
    _append_snapshot_manifest(snapshot_dir, entry)
    return entry


def _snapshot_skipped(
    snapshot_dir: Path,
    *,
    case: str,
    target_jobs: int,
    phase: str,
    reason: str,
    database: Path,
) -> dict[str, object]:
    entry = _abstained_snapshot(
        case=case,
        target_jobs=target_jobs,
        phase=phase,
        reason=reason,
        database=database,
    )
    entry["status"] = "skipped"
    _append_snapshot_manifest(snapshot_dir, entry)
    return entry


def _snapshot_target(
    path: Path | None,
    *,
    repository_root: Path,
    temp_root: Path,
) -> Path | None:
    """Validate a new retained-snapshot root without creating it yet."""

    if path is None:
        return None
    if not path.is_absolute():
        raise BenchmarkConfigurationError("--snapshot-dir must be an absolute path")
    selected = path.expanduser().resolve()
    _reject_product_path(selected, label="--snapshot-dir", repository_root=repository_root)
    if _is_within(selected, temp_root) or _is_within(temp_root, selected):
        raise BenchmarkConfigurationError(
            "--snapshot-dir must be separate from --temp-root"
        )
    if selected.exists() or selected.is_symlink():
        raise BenchmarkConfigurationError(
            f"--snapshot-dir must be a new path with no collision: {selected}"
        )
    return selected


def _initialize_snapshot_manifest(
    snapshot_dir: Path,
    *,
    repository_root: Path,
    temp_root: Path,
    cases: Sequence[str],
    sizes: Sequence[int],
) -> None:
    """Create the exclusive snapshot manifest before the first copy."""

    created = False
    try:
        snapshot_dir.mkdir(parents=True, mode=0o700)
        created = True
        manifest_path = snapshot_dir / "snapshot-manifest.jsonl"
        header = {
            "kind": "manifest",
            "schema": SNAPSHOT_SCHEMA,
            "repository_root": str(repository_root),
            "temp_root": str(temp_root),
            "snapshot_root": str(snapshot_dir),
            "cases": list(cases),
            "sizes": list(sizes),
            "copy_mode": "exclusive_byte_copy_after_closed_phase",
            "sidecar_policy": "wal_shm_journal_absent_or_empty; no_checkpoint",
            "source_fence": "lstat-before-and-after-copy plus destination SHA-256",
            "process_io_timing_includes_snapshot": False,
        }
        with manifest_path.open("x", encoding="utf-8") as stream:
            _write_jsonl(stream, header)
    except BaseException:
        if created:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
        raise


def _output_path(
    output: Path | None,
    output_dir: Path | None,
    *,
    timestamp: str,
    repository_root: Path,
    temp_root: Path,
    snapshot_dir: Path | None,
) -> Path | None:
    if output is not None and output_dir is not None:
        raise BenchmarkConfigurationError("use only one of --output or --output-dir")
    if output is None and output_dir is None:
        return None
    if output is not None:
        if str(output) == "-":
            return None
        if not output.is_absolute():
            raise BenchmarkConfigurationError("--output must be absolute or '-' ")
        selected = output.expanduser().resolve()
    else:
        if not output_dir.is_absolute():
            raise BenchmarkConfigurationError("--output-dir must be absolute")
        directory = output_dir.expanduser().resolve()
        _reject_product_path(directory, label="output", repository_root=repository_root)
        if _is_within(directory, temp_root):
            raise BenchmarkConfigurationError("output must not be inside --temp-root")
        if snapshot_dir is not None and _is_within(directory, snapshot_dir):
            raise BenchmarkConfigurationError("output must not be inside --snapshot-dir")
        selected = directory / f"semantic-generation-control-{timestamp}-{os.getpid()}.jsonl"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_product_path(selected, label="output", repository_root=repository_root)
    if _is_within(selected, temp_root):
        raise BenchmarkConfigurationError("output must not be inside --temp-root")
    if snapshot_dir is not None and _is_within(selected, snapshot_dir):
        raise BenchmarkConfigurationError("output must not be inside --snapshot-dir")
    if selected.exists() or selected.is_symlink():
        raise BenchmarkConfigurationError(
            f"output already exists; choose a fresh nonce path: {selected}"
        )
    selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return selected


def _write_jsonl(stream: Any, value: Mapping[str, object]) -> None:
    stream.write(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    stream.write("\n")
    stream.flush()
    if stream is not sys.stdout and hasattr(stream, "fileno"):
        os.fsync(stream.fileno())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default=",".join(map(str, DEFAULT_SIZES)))
    parser.add_argument(
        "--case",
        choices=(
            "all",
            "identical",
            "metadata",
            "smallchange",
            "move",
            "rename",
            "delete",
            "model_signature_change",
            "chunking_signature_change",
        ),
        default="identical",
    )
    parser.add_argument("--point-time-budget", type=float, default=DEFAULT_POINT_SECONDS)
    parser.add_argument("--group-time-budget", type=float, default=DEFAULT_GROUP_SECONDS)
    parser.add_argument("--repository-root", type=Path, default=DEFAULT_REPOSITORY_ROOT)
    parser.add_argument(
        "--temp-root",
        "--work-root",
        dest="temp_root",
        type=Path,
        help="absolute private parent for run state; default is a fresh system-temporary root",
    )
    parser.add_argument(
        "--trace-python-memory",
        action="store_true",
        help="opt in to tracemalloc (adds measurable Python allocation overhead)",
    )
    parser.add_argument(
        "--logical-hashes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "fixture-only logical typed-cell/row hashes and orphan counts; "
            "diagnostics are outside phase SQL/VM timers (default: enabled)"
        ),
    )
    parser.add_argument(
        "--process-io",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="capture /proc/self/io deltas around each performance phase (default: enabled)",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        help=(
            "opt-in new private root for closed SQLite fixture snapshots; it must be "
            "outside the checkout and --temp-root"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="absolute JSONL output path, or '-' for stdout; default is stdout",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="absolute directory for an automatically named JSONL output",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    global COMPONENTS
    parser = _parser()
    args = parser.parse_args(arguments)
    repository_root = Path(args.repository_root).expanduser().resolve()
    selected_cases = [
        case for case in CASE_NAMES if args.case in {"all", case}
    ]
    temp_parent: Path | None = None
    run_root: Path | None = None
    snapshot_dir: Path | None = None
    snapshot_created = False
    owns_temp_parent = False
    try:
        # SQLite otherwise swallows exceptions from trace/progress callbacks,
        # which could turn a broken diagnostic into a false zero count.
        sqlite3.enable_callback_tracebacks(True)
        sizes = _parse_sizes(args.sizes)
        point_seconds = _parse_point_seconds(args.point_time_budget)
        group_seconds = _parse_group_seconds(args.group_time_budget)
        temp_parent, owns_temp_parent = _effective_work_root(
            args.temp_root,
            repository_root=repository_root,
        )
        timestamp = _datetime.datetime.now(_datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        run_root = Path(
            tempfile.mkdtemp(
                prefix=f"semantic-generation-control-{timestamp}-{os.getpid()}-",
                dir=os.fspath(temp_parent),
            )
        )
        _private_environment(run_root)
        snapshot_dir = _snapshot_target(
            args.snapshot_dir,
            repository_root=repository_root,
            temp_root=temp_parent,
        )
        output_path = _output_path(
            args.output,
            args.output_dir,
            timestamp=timestamp,
            repository_root=repository_root,
            temp_root=temp_parent,
            snapshot_dir=snapshot_dir,
        )
        if snapshot_dir is not None:
            _initialize_snapshot_manifest(
                snapshot_dir,
                repository_root=repository_root,
                temp_root=temp_parent,
                cases=selected_cases,
                sizes=sizes,
            )
            snapshot_created = True
        COMPONENTS = _load_components(repository_root)
    except (BenchmarkConfigurationError, OSError) as exc:
        if run_root is not None:
            shutil.rmtree(run_root, ignore_errors=True)
        if snapshot_created and snapshot_dir is not None:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
        if temp_parent is not None and owns_temp_parent:
            shutil.rmtree(temp_parent, ignore_errors=True)
        parser.error(str(exc))

    manifest = {
        "kind": "manifest",
        "schema": BENCHMARK_SCHEMA,
        "benchmark": "semantic_generation_control",
        "repository_root": str(repository_root),
        "source_mode": "synthetic-in-memory-records",
        "backend_contract": FIXTURE_BACKEND_CONTRACT,
        "backend_batch_size": FIXTURE_BACKEND_BATCH_SIZE,
        "model_real": False,
        "sizes": list(sizes),
        "cases": selected_cases,
        "point_time_budget_seconds": point_seconds,
        "group_time_budget_seconds": group_seconds,
        "nominal_fixture": {
            "items": EXPECTED_NOMINAL_ITEMS,
            "chunks": EXPECTED_NOMINAL_JOBS,
            "jobs": EXPECTED_NOMINAL_JOBS,
            "two_chunks_per_item": True,
        },
        "fixture_contract": {
            "source_kind": SOURCE_KIND,
            "body_text_template": _body_text(0),
            "model_signature": "semantic-audit-deterministic-v1",
            "model_vector_space": "semantic-audit-deterministic-space-v1",
            "model_id": "fixture/semantic-audit-v1",
            "model_version": "contract-fixture-v1",
            "model_dimensions": 4,
            "model_provider": "test-deterministic",
            "chunking": {
                "max_chars": 512,
                "max_terms": 96,
                "overlap_chars": 0,
                "overlap_terms": 0,
                "min_natural_break_chars": 64,
                "algorithm_version": "semantic-audit-natural-window-v2",
                "model_token_limit": 512,
                "tokenizer_signature": "semantic-audit-deterministic-tokenizer-v1",
            },
            "lifecycle_order": list(LIFECYCLE_ORDER),
        },
        "external_resource_caps": {
            "runtime_max_seconds": int(MAX_GROUP_SECONDS),
            "memory_bytes": EXTERNAL_MEMORY_CAP_BYTES,
            "temporary_bytes": EXTERNAL_TEMP_CAP_BYTES,
            "enforcement": "coordinator-bwrap-or-host-runner",
        },
        "trace_python_memory": bool(args.trace_python_memory),
        "logical_hashes": bool(args.logical_hashes),
        "hashes_outside_performance_window": True,
        "logical_integrity_schema": LOGICAL_INTEGRITY_SCHEMA,
        "process_io_capture": bool(args.process_io),
        "process_io_semantics": (
            "deltas sampled immediately around the phase; unavailable is not zero"
        ),
        "snapshots": {
            "enabled": snapshot_dir is not None,
            "schema": SNAPSHOT_SCHEMA,
            "root": None if snapshot_dir is None else str(snapshot_dir),
            "default": False,
            "copy_scope": "fixture-owned SQLite only; after closed phase and diagnostics",
            "sidecar_policy": "WAL/SHM/journal absent or empty; no checkpoint",
            "fence_policy": "lstat before and after byte copy plus destination SHA-256",
            "performance_io_includes_snapshot": False,
        },
        "temp_root": str(temp_parent),
        "run_root": str(run_root),
        "temporary_state": True,
        "environment_isolated": True,
        "expected_point_count": len(selected_cases) * len(sizes),
        "result_path": "-" if output_path is None else str(output_path),
    }

    stream = sys.stdout
    close_stream = False
    if output_path is not None:
        stream = output_path.open("w", encoding="utf-8")
        close_stream = True
    try:
        _write_jsonl(stream, manifest)
        group_started = time.monotonic()
        group_stopped = False
        point_failures: list[dict[str, object]] = []
        completed_point_count = 0
        for case in selected_cases:
            for target_jobs in sizes:
                if time.monotonic() - group_started >= group_seconds:
                    _write_jsonl(
                        stream,
                        {
                            "kind": "group_stop",
                            "schema": BENCHMARK_SCHEMA,
                            "reason": "group_time_budget",
                            "group_time_budget_seconds": group_seconds,
                            "elapsed_seconds": time.monotonic() - group_started,
                        },
                    )
                    group_stopped = True
                    break
                with tempfile.TemporaryDirectory(
                    prefix=f"neocortex-semantic-generation-{case}-{target_jobs}-",
                    dir=os.fspath(run_root),
                ) as temporary:
                    try:
                        result = _run_point(
                            components=_components(),
                            target_jobs=target_jobs,
                            case=case,
                            point_seconds=point_seconds,
                            trace_python_memory=bool(args.trace_python_memory),
                            logical_hashes=bool(args.logical_hashes),
                            capture_process_io=bool(args.process_io),
                            snapshot_dir=snapshot_dir,
                            work_root=Path(temporary),
                        )
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        result = {
                            "schema": BENCHMARK_SCHEMA,
                            "case": case,
                            "target_jobs": target_jobs,
                            "status": "error",
                            "error": {"type": type(exc).__name__, "message": str(exc)[:1_000]},
                        }
                    completed_point_count += 1
                    failure_reason = _point_failure_reason(
                        result,
                        logical_hashes=bool(args.logical_hashes),
                    )
                    if failure_reason is not None:
                        point_failures.append(
                            {
                                "case": case,
                                "target_jobs": target_jobs,
                                "reason": failure_reason,
                            }
                        )
                    _write_jsonl(stream, result)
            if group_stopped:
                break
        if group_stopped or completed_point_count != len(selected_cases) * len(sizes):
            point_failures.append(
                {
                    "reason": "group_incomplete",
                    "completed_points": completed_point_count,
                    "expected_points": len(selected_cases) * len(sizes),
                }
            )
        _write_jsonl(
            stream,
            {
                "kind": "run_summary",
                "schema": BENCHMARK_SCHEMA,
                "status": "complete" if not point_failures else "failed",
                "completed_points": completed_point_count,
                "expected_points": len(selected_cases) * len(sizes),
                "point_failures": point_failures,
            },
        )
    finally:
        if close_stream:
            stream.close()
        if owns_temp_parent and temp_parent is not None:
            shutil.rmtree(temp_parent, ignore_errors=True)
    return 1 if point_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
