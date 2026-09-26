"""Semantic application workflows used by the flat CLI.

The CLI adapter owns dispatch compatibility and the domain module owns
Semantic budgeting, source selection, publication/recovery coordination, and
operation execution.  Human-readable lines remain intentionally stable for
the existing public command contract.
"""

from __future__ import annotations
import argparse
import json
import math
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress
from neocortex.persistence.state_publication import StatePublicationRecoveryRequired
from neocortex.api.read_contract import sanitize_untrusted_text
from neocortex.platform.policy import stat_birthtime_ns

if TYPE_CHECKING:
    from neocortex.persistence.state_publication import StateOwnerHead
    from neocortex.semantic.semantic_models import EmbeddingModelSpec
    from neocortex.semantic.semantic_exact_index import ExactIndexHandle
    from neocortex.semantic.semantic_publication_heads import _SemanticOwnerLease
    from neocortex.semantic.semantic_service_contracts import SemanticIndexResult
    from neocortex.semantic.semantic_work_budget import SemanticWorkBudget

__all__ = [
    "prepare_integrated_semantic_start",
    "recover_pending_integrated_semantic",
    "run_integrated_all_semantic_index",
    "run_semantic_classify",
    "run_semantic_evidence",
    "run_semantic_exact_index_build",
    "run_semantic_image_calibrate",
    "run_semantic_index",
    "run_semantic_plan",
    "run_semantic_prepare_models",
    "run_semantic_search",
    "run_semantic_status",
    "semantic_resume_available",
]

_INTEGRATED_START_METADATA_TIMEOUT_SECONDS = 60.0
_INTEGRATED_START_SNAPSHOT_BYTES = 256 * 1024 * 1024
_INTEGRATED_FRAMEWORK_CONTROL_READ_TIMEOUT_SECONDS = 1.0
_EXACT_INDEX_MAX_ROWS = 500_000
_EXACT_INDEX_MAX_TOTAL_BYTES = 4_000_000_000


class _SemanticSearchKeywordArgs(TypedDict):
    """Typed keyword set for the public Semantic search facade."""

    limit: int
    max_vectors: int
    include_text: bool
    include_images: bool
    include_lexical: bool
    text_model: EmbeddingModelSpec | None
    model_cache: Path | None
    local_files_only: bool
    threads: int | None
    diagnostic_item_ids: tuple[str, ...]
    cancellation_check: NotRequired[Callable[[], None]]
    exact_index: NotRequired[ExactIndexHandle]

# region [01] Multimodal semantic index


def _console_text(value: str) -> str:
    """Keep corpus-derived CLI output printable on legacy Windows consoles."""

    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        return value
    try:
        value.encode(encoding)
    except UnicodeEncodeError:
        return value.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:  # pragma: no cover - defensive custom stream support
        return value
    return value


def _print_console_line(value: str) -> None:
    print(_console_text(value))


def _semantic_text_model(profile: str):
    from neocortex.semantic.semantic_config import (
        compact_multilingual_text_model,
        multilingual_text_model,
    )

    return compact_multilingual_text_model() if profile == "compact" else multilingual_text_model()


def _persisted_semantic_admission_policy(args: argparse.Namespace):
    """Load the latest corpus policy without inventing a second state owner.

    Direct Semantic commands may run before Framework state exists.  In that
    case ``None`` deliberately preserves the service's empty-policy default;
    an existing but malformed policy is surfaced as an error instead of being
    silently bypassed.
    """

    current = getattr(args, "_semantic_admission_policy", None)
    if current is not None:
        return current
    database = args.state_directory / "framework.sqlite3"
    if not database.is_file():
        return None
    from neocortex.persistence.framework_state_writer import FrameworkState

    corpus = getattr(args, "root", args.state_directory)
    with FrameworkState(database) as state:
        ledger = state.content_admission_ledger()
        stored = ledger.read_policy(corpus)
    if stored is None:
        return None
    args._semantic_admission_policy = stored.policy
    return stored.policy


def _validate_semantic_state_write(
    state_directory: Path,
    *,
    database: bool,
    extra_paths: tuple[Path, ...] = (),
) -> None:
    """Reject protected state targets before mkdir, lock, or SQLite opens."""

    from neocortex.safety.internal_paths import canonical_internal_paths_policy
    from neocortex.integrations.inventory.inventory_boundary import (
        state_sqlite_mutation_paths,
        validate_authorized_state_path,
    )
    from neocortex.safety.protected_content import canonical_protected_content_policy
    from neocortex.semantic.semantic_service import SEMANTIC_DATABASE_NAME

    database_paths = (
        state_sqlite_mutation_paths(state_directory / SEMANTIC_DATABASE_NAME) if database else ()
    )
    validate_authorized_state_path(
        state_directory,
        internal_paths_policy=canonical_internal_paths_policy(),
        protected_content_policy=canonical_protected_content_policy(),
        mutation_paths=(
            state_directory / "framework.lock",
            *database_paths,
            *extra_paths,
        ),
    )


def _semantic_failure(
    label: str,
    exc: BaseException,
    *,
    offline: bool,
    print_output: bool = True,
) -> int:
    if print_output:
        print(f"ERROR {label} {type(exc).__name__}: {exc}")
    if offline and print_output:
        print(
            "HINT this action is offline-only; provision the required complete local "
            "snapshot and select it with --semantic-model-cache. Explicit model "
            "preparation may download files and belongs outside an offline run."
        )
    return 2


def _selected_semantic_text_sources(args: argparse.Namespace) -> tuple[str, ...]:
    from neocortex.semantic.semantic_sources import TEXT_SOURCE_KINDS, semantic_source_database

    if args.semantic_source is not None:
        return tuple(dict.fromkeys(args.semantic_source))
    source_kinds = tuple(TEXT_SOURCE_KINDS)
    return tuple(
        source_kind
        for source_kind in source_kinds
        if semantic_source_database(args.state_directory, source_kind).is_file()
    )


def _print_semantic_index_result(scope: str, result) -> None:
    print(
        f"SEMANTIC_INDEX scope={scope} sources={','.join(result.sources)} "
        f"mode={result.execution_mode} sources_reused={result.sources_reused} "
        f"sources_enumerated={result.sources_enumerated} "
        f"items={result.items_staged} chunks={result.chunks_staged} "
        f"new_jobs={result.new_jobs_staged} "
        f"errors={result.errors} stale={result.stale} "
        f"incomplete={result.incomplete} complete={int(result.complete)} "
        f"truncated={int(result.truncated)} "
        f"truncation_reason={result.truncation_reason or '-'} "
        f"database={result.semantic_database}"
    )
    for work in result.generations:
        summary = work.summary
        print(
            f"SEMANTIC_GENERATION id={summary.generation_id} "
            f"status={summary.status} model={summary.model_signature} "
            f"queued={work.queued} reused={work.reused} embedded={work.embedded} "
            f"failed={work.failed} pending={summary.pending} leased={summary.leased} "
            f"errors={summary.errors} stale={summary.stale} stop_reason={getattr(work, 'stop_reason', None) or '-'}"
        )


@dataclass(slots=True)
class _SemanticIndexExecution:
    args: argparse.Namespace
    text_model: EmbeddingModelSpec
    selected_sources: tuple[str, ...]
    work_budget: SemanticWorkBudget
    progress: ProgressCallback | None
    result_sink: Callable[[str, object], None] | None
    results: list[tuple[str, SemanticIndexResult]] = field(default_factory=list)
    scope_timings: list[tuple[str, int]] = field(default_factory=list)
    unavailable_scopes: dict[str, str] = field(default_factory=dict)
    writer_coordinated: bool = False


@dataclass(frozen=True, slots=True)
class _PendingIntegratedMetadata:
    """Bounded metadata needed to restart one stale integrated checkpoint."""

    event_id: str
    expected_epoch: int
    pending_owners: tuple[str, ...]
    previous_owners: tuple[str, ...]
    manifest: Mapping[str, object] | None


class _IntegratedStartReadBudget:
    """One cooperative budget for the fresh-start metadata preflight."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        cancellation_check: Callable[[], bool | None] | None,
        clock: Callable[[], float],
        metadata_timeout_seconds: float | None,
    ) -> None:
        if not callable(clock):
            raise TypeError("preflight clock must be callable")
        if cancellation_check is not None and not callable(cancellation_check):
            raise TypeError("preflight cancellation check must be callable")
        timeout = (
            _INTEGRATED_START_METADATA_TIMEOUT_SECONDS
            if metadata_timeout_seconds is None
            else metadata_timeout_seconds
        )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0.0
        ):
            raise ValueError("preflight metadata timeout must be finite and positive")
        caps = [float(timeout)]
        for name in ("run_time_budget_seconds", "semantic_time_budget_seconds"):
            value = getattr(args, name, None)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise ValueError(f"{name} must be finite and positive")
            caps.append(float(value))
        self.clock = clock
        self.cancellation_check = cancellation_check
        self.started = clock()
        self.deadline = self.started + min(caps)

    def check(self) -> None:
        if self.cancellation_check is not None:
            decision = self.cancellation_check()
            if decision is True:
                raise KeyboardInterrupt("Semantic start preflight was cancelled")
            if decision is not None and decision is not False:
                raise KeyboardInterrupt("Semantic start preflight was cancelled")
        if self.clock() >= self.deadline:
            from neocortex.persistence.framework_state_writer import RunBudgetExceeded

            raise RunBudgetExceeded("time")

    def remaining_seconds(self) -> float:
        self.check()
        remaining = self.deadline - self.clock()
        if remaining <= 0.0:
            from neocortex.persistence.framework_state_writer import RunBudgetExceeded

            raise RunBudgetExceeded("time")
        return remaining

    def snapshot_budget(self):
        from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudget

        remaining = self.remaining_seconds()
        return SQLiteSnapshotBudget(
            max_temporary_bytes=_INTEGRATED_START_SNAPSHOT_BYTES,
            prepare_timeout_seconds=min(
                _INTEGRATED_START_METADATA_TIMEOUT_SECONDS,
                remaining,
            ),
            cancellation_check=self._snapshot_checkpoint,
            monotonic_clock=self.clock,
        )

    def _snapshot_checkpoint(self) -> bool:
        self.check()
        return False

    def apply_elapsed_to_explicit_caps(self, args: argparse.Namespace) -> None:
        """Charge preflight time without consuming item/job metadata budgets."""

        self.check()
        elapsed = max(0.0, self.clock() - self.started)
        for name in ("run_time_budget_seconds", "semantic_time_budget_seconds"):
            value = getattr(args, name, None)
            if value is None:
                continue
            remaining = float(value) - elapsed
            if remaining <= 0.0:
                from neocortex.persistence.framework_state_writer import RunBudgetExceeded

                raise RunBudgetExceeded("time")
            setattr(args, name, remaining)


@contextmanager
def _bounded_framework_metadata_read(
    database: Path,
    controls: _IntegratedStartReadBudget,
):
    """Read Framework metadata through the fenced SQLite kernel."""

    controls.check()
    if not database.is_file():
        raise StatePublicationRecoveryRequired("original Framework manifest is unavailable")
    from neocortex.persistence.sqlite_immutable import (
        SQLiteReadSession,
        preferred_sqlite_read_mode,
    )

    timeout = min(_INTEGRATED_START_METADATA_TIMEOUT_SECONDS, controls.remaining_seconds())
    session = SQLiteReadSession(
        database,
        mode=preferred_sqlite_read_mode(database),
        timeout_seconds=timeout,
        max_attempts=2,
        budget=controls.snapshot_budget(),
    )
    with session as connection:
        controls.check()
        failure: BaseException | None = None

        def progress() -> int:
            nonlocal failure
            try:
                controls.check()
            except BaseException as exc:
                failure = exc
                return 1
            return 0

        connection.set_progress_handler(progress, 1_000)
        try:
            yield connection
        except sqlite3.OperationalError as exc:
            if failure is not None:
                raise failure from exc
            raise
        finally:
            connection.set_progress_handler(None, 0)
        if failure is not None:
            raise failure
        controls.check()


def _exact_index_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _print_exact_index_result(
    operation: str,
    directory: Path,
    summary: Mapping[str, object],
    usage: Mapping[str, object],
) -> None:
    _print_console_line(
        f"{operation} directory={json.dumps(str(directory), ensure_ascii=False)} "
        f"summary={_exact_index_json(dict(summary))} "
        f"usage={_exact_index_json(dict(usage))}"
    )


def _print_exact_index_usage(directory: Path, handle: object) -> None:
    try:
        usage_summary = getattr(handle, "usage_summary", None)
        if not callable(usage_summary):
            raise TypeError("exact-index handle has no usage_summary")
        usage = usage_summary()
    except Exception as exc:  # diagnostics must not hide a completed search
        _print_console_line(
            f"WARNING semantic-exact-index usage unavailable "
            f"directory={json.dumps(str(directory), ensure_ascii=False)} "
            f"error={type(exc).__name__}"
        )
        return
    if not isinstance(usage, Mapping):
        _print_console_line(
            f"WARNING semantic-exact-index usage has invalid shape "
            f"directory={json.dumps(str(directory), ensure_ascii=False)}"
        )
        return
    _print_console_line(
        f"SEMANTIC_EXACT_INDEX_USAGE directory={json.dumps(str(directory), ensure_ascii=False)} "
        f"usage={_exact_index_json(dict(usage))}"
    )


def _close_exact_index_handle(directory: Path, handle: object) -> None:
    """Release an explicitly opened handle without masking the CLI result."""

    try:
        close = getattr(handle, "close", None)
        if not callable(close):
            raise TypeError("exact-index handle has no close")
        close()
    except Exception as exc:  # cleanup diagnostics must not mask the operation result
        _print_console_line(
            f"WARNING semantic-exact-index close unavailable "
            f"directory={json.dumps(str(directory), ensure_ascii=False)} "
            f"error={type(exc).__name__}"
        )


def _semantic_cancellation_checkpoint(
    args: argparse.Namespace,
) -> Callable[[], None] | None:
    """Adapt the CLI's boolean cancellation signal to the exact-index API."""

    cancellation: Callable[[], bool | None] | None = getattr(
        args, "_semantic_cancellation_check", None
    )
    if cancellation is None:
        return None
    if not callable(cancellation):
        raise TypeError("Semantic cancellation check must be callable")

    def checkpoint() -> None:
        decision = cancellation()
        if decision is True or (decision is not None and decision is not False):
            raise KeyboardInterrupt("Semantic exact-index operation was cancelled")

    return checkpoint


def run_semantic_exact_index_build(args: argparse.Namespace) -> int:
    """Build one explicit derived exact-index artifact without mutating Semantic state."""

    from neocortex.semantic.semantic_exact_index import prepare_exact_index
    from neocortex.semantic.semantic_service import SEMANTIC_DATABASE_NAME

    database = args.state_directory / SEMANTIC_DATABASE_NAME
    cancellation_check = _semantic_cancellation_checkpoint(args)
    handle: ExactIndexHandle | None = None
    try:
        handle = prepare_exact_index(
            database,
            args.semantic_exact_index_build,
            model_signature=args.semantic_exact_index_model,
            text_scope=args.semantic_exact_index_scope,
            max_rows=args.semantic_max_vectors,
            max_total_bytes=_EXACT_INDEX_MAX_TOTAL_BYTES,
            cancellation_check=cancellation_check,
        )
        summary = handle.summary()
        usage = handle.usage_summary()
        if not isinstance(summary, Mapping) or not isinstance(usage, Mapping):
            raise TypeError("exact-index handle returned an invalid summary")
        _print_exact_index_result(
            "SEMANTIC_EXACT_INDEX_BUILD",
            args.semantic_exact_index_build,
            summary,
            usage,
        )
        return 0
    except Exception as exc:
        return _semantic_failure("semantic-exact-index-build", exc, offline=False)
    finally:
        if handle is not None:
            _close_exact_index_handle(args.semantic_exact_index_build, handle)


def _open_exact_index_for_search(
    args: argparse.Namespace,
    database: Path,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> ExactIndexHandle | None:
    """Open one validated exact-index handle, or return None for typed fallback."""

    if args.semantic_exact_index is None:
        return None
    if cancellation_check is None:
        cancellation_check = _semantic_cancellation_checkpoint(args)
    from neocortex.semantic.semantic_exact_index import ExactIndexUnavailable, open_exact_index

    _print_console_line(
        "NOTICE semantic-exact-index cold validation is bounded and may scan source; "
        "only reused handle query is warm"
    )
    started = time.monotonic()
    try:
        handle = open_exact_index(
            database,
            args.semantic_exact_index,
            max_rows=_EXACT_INDEX_MAX_ROWS,
            max_total_bytes=_EXACT_INDEX_MAX_TOTAL_BYTES,
            cancellation_check=cancellation_check,
        )
    except ExactIndexUnavailable as exc:
        _print_console_line(
            "WARNING semantic-exact-index unavailable; falling back to normal semantic search "
            f"reason={json.dumps(str(exc), ensure_ascii=False)}"
        )
        return None
    elapsed = max(0.0, time.monotonic() - started)
    _print_console_line(
        f"SEMANTIC_EXACT_INDEX_OPEN directory={json.dumps(str(args.semantic_exact_index), ensure_ascii=False)} "
        "cold_validation=bounded_may_scan_source only_reused_handle_query_is_warm=1 "
        f"elapsed_seconds={elapsed:.6f}"
    )
    return handle


def run_semantic_status(args: argparse.Namespace) -> int:
    """Show bounded semantic state without creating or migrating it."""

    from neocortex.semantic.semantic_service import SEMANTIC_DATABASE_NAME, semantic_status

    try:
        status = semantic_status(args.state_directory)
    except Exception as exc:  # direct diagnostics must not leak library tracebacks
        return _semantic_failure("semantic-status", exc, offline=False)
    database = args.state_directory / SEMANTIC_DATABASE_NAME
    if not status.exists:
        print(f"SEMANTIC_STATUS exists=0 database={database}")
        return 0
    counts = ",".join(f"{name}:{value}" for name, value in sorted(status.counts.items()))
    print(
        f"SEMANTIC_STATUS exists=1 schema={status.schema_version} "
        f"counts={counts or '-'} database={database}"
    )
    for summary in status.generations:
        cursor = json.dumps(
            summary.cursor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        print(
            f"SEMANTIC_GENERATION id={summary.generation_id} "
            f"status={summary.status} model={summary.model_signature} "
            f"pending={summary.pending} leased={summary.leased} done={summary.done} "
            f"errors={summary.errors} stale={summary.stale} cursor={cursor}"
        )
        timing = status.generation_timings.get(summary.generation_id)
        if timing is not None:
            print(
                f"SEMANTIC_TIMING generation={summary.generation_id} "
                f"duration_ns={timing['generation_duration_ns']} "
                f"executed_ns={timing['executed_ns']} "
                f"cache_hit_ns={timing['cache_hit_ns']} "
                f"replay_ns={timing['replay_ns']} "
                f"receipts={timing['receipts']} basis=owner_receipt"
            )
    return 0


def run_semantic_plan(args: argparse.Namespace) -> int:
    """Project Semantic work without creating locks, models, jobs or state."""

    from neocortex.semantic.semantic_service import plan_semantic_index, semantic_plan_payload

    selected_sources = _selected_semantic_text_sources(args)
    if args.semantic_plan in {"text", "all"} and not selected_sources:
        return _semantic_failure(
            "semantic-plan",
            FileNotFoundError(
                "no durable PDF, DOCX, Office, audio or text cache is available"
            ),
            offline=False,
        )
    try:
        plan = plan_semantic_index(
            args.state_directory,
            scope=args.semantic_plan,
            source_kinds=selected_sources,
            text_model=_semantic_text_model(args.semantic_text_profile),
            embed_ocr_text=not args.semantic_no_ocr,
            max_scratch_bytes=args.semantic_plan_max_scratch_bytes,
        )
    except Exception as exc:  # every owner failure is an explicit blocked plan
        return _semantic_failure("semantic-plan", exc, offline=False)
    if args.semantic_plan_json:
        print(
            json.dumps(
                semantic_plan_payload(plan),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    seconds_lower = (
        "-"
        if plan.estimated_model_seconds_lower_bound is None
        else f"{plan.estimated_model_seconds_lower_bound:.6f}"
    )
    seconds_upper = (
        "-"
        if plan.estimated_model_seconds_upper_bound is None
        else f"{plan.estimated_model_seconds_upper_bound:.6f}"
    )
    print(
        f"SEMANTIC_PLAN dry_run=1 complete={int(plan.complete)} "
        "state_mutated=0 jobs_created=0 "
        f"scope={plan.scope} sources={','.join(plan.selected_sources) or '-'} "
        f"resources={plan.resources} sections={plan.sections} chunks={plan.chunks} "
        f"entities={plan.embedding_entities} unique={plan.unique_contents} "
        f"reusable={plan.reusable_unique_contents} new={plan.new_unique_contents} "
        f"input_bytes={plan.input_bytes} unique_input_bytes={plan.unique_input_bytes} "
        f"vector_blob_bytes_lower_bound="
        f"{plan.new_vector_blob_bytes_lower_bound} "
        f"request_lower={plan.model_request_contents_lower_bound} "
        f"request_upper={plan.model_request_contents_upper_bound} "
        f"model_seconds_lower={seconds_lower} model_seconds_upper={seconds_upper} "
        f"cost_calibrated={int(plan.cost_calibrated)} "
        f"cost_complete={int(plan.cost_complete)} "
        f"originals_verified="
        f"{'unknown' if plan.originals_verified is None else int(plan.originals_verified)} "
        f"execution_ready="
        f"{'unknown' if plan.execution_ready is None else int(plan.execution_ready)} "
        f"sqlite_shm_side_effect=possible signature={plan.plan_signature}"
    )
    for source in plan.source_plans:
        print(
            f"SEMANTIC_PLAN_SOURCE name={source.source_kind} "
            f"schema={source.schema_version} resources={source.resources} "
            f"sections={source.sections} chunks={source.chunks} "
            f"entities={source.embedding_entities} source_bytes={source.source_bytes} "
            f"section_text_bytes={source.section_text_bytes} "
            f"input_bytes={source.input_bytes} database={source.database}"
        )
    for workload in plan.workloads:
        workload_seconds_lower = (
            "-"
            if workload.estimated_model_seconds_lower_bound is None
            else f"{workload.estimated_model_seconds_lower_bound:.6f}"
        )
        workload_seconds_upper = (
            "-"
            if workload.estimated_model_seconds_upper_bound is None
            else f"{workload.estimated_model_seconds_upper_bound:.6f}"
        )
        print(
            f"SEMANTIC_PLAN_WORKLOAD name={workload.name} "
            f"modality={workload.modality} role={workload.role} "
            f"model={workload.model_signature} model_id={workload.model_id} "
            f"model_version={workload.model_version} provider={workload.provider} "
            f"vector_space={workload.vector_space} dimensions={workload.dimensions} "
            f"dtype={workload.vector_dtype} entities={workload.embedding_entities} "
            f"unique={workload.unique_contents} "
            f"preexisting_reuse={workload.preexisting_reusable_contents} "
            f"planned_reuse={workload.planned_reusable_contents} "
            f"new={workload.new_unique_contents} input_bytes={workload.input_bytes} "
            f"unique_input_bytes={workload.unique_input_bytes} "
            f"vector_blob_bytes_lower_bound="
            f"{workload.new_vector_blob_bytes_lower_bound} "
            f"request_lower={workload.model_request_contents_lower_bound} "
            f"request_upper={workload.model_request_contents_upper_bound} "
            f"model_seconds_lower={workload_seconds_lower} "
            f"model_seconds_upper={workload_seconds_upper} "
            f"cost_calibrated={int(workload.cost_calibrated)} "
            f"cost_basis={workload.cost_calibration_signature or '-'} "
            f"cost_unavailable={workload.cost_unavailable_reason or '-'}"
        )
    return 0


def run_semantic_prepare_models(args: argparse.Namespace) -> int:
    """Explicitly acquire production model weights under the framework lock."""

    from neocortex.runtime.control.locking import FrameworkRunLock
    from neocortex.semantic.semantic_config import default_semantic_model_cache
    from neocortex.semantic.semantic_service import prepare_semantic_models

    try:
        model_cache = (
            default_semantic_model_cache(args.state_directory)
            if args.semantic_model_cache is None
            else args.semantic_model_cache
        )
        _validate_semantic_state_write(
            args.state_directory,
            database=False,
            extra_paths=(model_cache,),
        )
        args.state_directory.mkdir(parents=True, exist_ok=True)
        with FrameworkRunLock(args.state_directory / "framework.lock"):
            prepared = prepare_semantic_models(
                args.state_directory,
                model_cache=args.semantic_model_cache,
                include_compact=args.semantic_include_compact,
                local_files_only=False,
                threads=args.semantic_threads,
            )
    except Exception as exc:  # model runtimes expose backend-specific exceptions
        return _semantic_failure("semantic-prepare-models", exc, offline=False)
    for model in prepared:
        print(
            f"SEMANTIC_MODEL_READY id={model.model_id} "
            f"signature={model.model_signature} dimensions={model.dimensions} "
            f"elapsed_seconds={model.elapsed_seconds:.3f}"
        )
    return 0


def run_semantic_index(
    args: argparse.Namespace,
    *,
    incomplete_is_error: bool = True,
    progress: ProgressCallback | None = None,
    result_sink: Callable[[str, object], None] | None = None,
    print_output: bool = True,
    framework_lock_held: bool = False,
) -> int:
    """Incrementally embed durable route state without authorizing downloads.

    ``framework_lock_held`` is an internal orchestration seam.  The integrated
    ``--all`` callback runs while its outer Framework lifecycle owns the lock;
    direct Semantic commands leave this false and acquire the lock here.
    """

    from neocortex.runtime.control.locking import FrameworkRunLock
    from neocortex.semantic.semantic_service import index_image_embeddings, index_text_embeddings
    from neocortex.semantic.semantic_work_budget import SemanticWorkBudget
    from neocortex.persistence.framework_state_writer import RunBudgetExceeded

    text_model = _semantic_text_model(str(getattr(args, "semantic_text_profile", "") or ""))
    selected_sources = _selected_semantic_text_sources(args)
    # Resolve the durable Framework-owned policy once per Semantic stage.  The
    # source callbacks then apply it before model work, while direct callers
    # without an existing Framework owner continue with an empty policy.
    _persisted_semantic_admission_policy(args)
    work_budget = getattr(args, "_semantic_work_budget", None)
    if work_budget is None:
        work_budget = SemanticWorkBudget.from_time_budget(
            max_items=args.semantic_max_items,
            max_new_jobs=args.semantic_max_new_jobs,
            time_budget_seconds=args.semantic_time_budget_seconds,
            cancellation_check=getattr(args, "_semantic_cancellation_check", None),
            preserve_existing_generations=bool(getattr(args, "_semantic_preserve_generations", False)),
            retry_recoverable_errors=bool(getattr(args, "all", False)),
        )
    elif not isinstance(work_budget, SemanticWorkBudget):
        raise TypeError("integrated Semantic work budget is invalid")
    execution = _SemanticIndexExecution(
        args=args,
        text_model=text_model,
        selected_sources=selected_sources,
        work_budget=work_budget,
        progress=progress,
        result_sink=result_sink,
    )
    execution.args._semantic_failure = None
    try:
        _validate_semantic_state_write(
            args.state_directory,
            database=True,
        )
        def execute_scopes() -> None:
            from neocortex.semantic.semantic_source_budget import semantic_source_read_budget
            from neocortex.semantic.semantic_service import admission_policy_scope

            _validate_integrated_publication_token(args)
            policy = getattr(args, "_semantic_admission_policy", None)
            # Both the integrated callback and the direct Semantic path are
            # inside FrameworkRunLock here.  Let owner-local Semantic reads
            # reuse the coordinated writer path instead of copying a large
            # published database through the public 256 MiB snapshot budget.
            execution.writer_coordinated = True
            with admission_policy_scope(policy):
                with semantic_source_read_budget(work_budget):
                    _execute_semantic_index_scopes(
                        execution,
                        text_operation=index_text_embeddings,
                        image_operation=index_image_embeddings,
                    )
        if framework_lock_held:
            execute_scopes()
        else:
            with FrameworkRunLock(args.state_directory / "framework.lock"):
                execute_scopes()
    except RunBudgetExceeded:
        raise
    except Exception as exc:  # model runtimes expose backend-specific exceptions
        return _semantic_index_failure(execution, exc, print_output=print_output)
    return _complete_semantic_index_execution(
        execution,
        incomplete_is_error=incomplete_is_error,
        print_output=print_output,
    )


def _validate_integrated_publication_token(args: argparse.Namespace) -> None:
    """Only the original producer may advance an unresolved publication."""

    from neocortex.persistence.state_publication import read_state_publication_state

    view = read_state_publication_state(args.state_directory)
    event_id = getattr(args, "_semantic_publication_event_id", None)
    if event_id is not None:
        if len(view.pending) != 1 or view.pending[0].event_id != event_id:
            raise StatePublicationRecoveryRequired("active Semantic publication changed")
        return
    if view.status not in {"absent", "complete"}:
        raise StatePublicationRecoveryRequired(view.reason or view.status)


def _observe_integrated_heads(
    state_directory: Path,
    *,
    work_budget=None,
    writer_connection: _SemanticOwnerLease | None = None,
):
    from neocortex.semantic.semantic_publication_heads import (
        PublicationHeadsError,
        _semantic_owner_lease,
        observe_integrated_owner_heads,
    )

    try:
        remaining = None if work_budget is None else work_budget.remaining_seconds()
        deadline = None if remaining is None else time.monotonic() + remaining
        cancellation = None if work_budget is None else work_budget.cancellation_check

        def lease_checkpoint() -> None:
            if cancellation is not None:
                decision = cancellation()
                if decision is not None and decision is not False:
                    raise PublicationHeadsError("Semantic owner lease was cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise PublicationHeadsError("Semantic owner lease deadline exceeded")

        def observe(connection: _SemanticOwnerLease | None):
            if connection is None:
                return observe_integrated_owner_heads(
                    state_directory,
                    deadline_monotonic=deadline,
                    cancellation_check=cancellation,
                )
            return observe_integrated_owner_heads(
                state_directory,
                deadline_monotonic=deadline,
                cancellation_check=cancellation,
                _writer_lease=connection,
            )

        if writer_connection is not None:
            return observe(writer_connection)
        with _semantic_owner_lease(
            state_directory,
            checkpoint=lease_checkpoint,
            timeout_seconds=60.0 if remaining is None else max(0.001, remaining),
        ) as owner:
            return observe(owner)
    except PublicationHeadsError as exc:
        raise StatePublicationRecoveryRequired(str(exc)) from exc


def _integrated_semantic_budget(args: argparse.Namespace, run_id: int | None):
    """Share explicit limits across all scopes and sample durable cancellation."""

    from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
    from neocortex.semantic.semantic_work_budget import (
        SemanticIndexDeadlineExceeded,
        SemanticWorkBudget,
    )

    max_items = args.semantic_max_items
    max_jobs = args.semantic_max_new_jobs
    duration = args.semantic_time_budget_seconds
    active_run = False
    lifecycle_snapshot = None
    global_deadline_ns: int | None = None
    if run_id is not None:
        with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
            row = state._connection.execute(
                "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            active_run = row is not None and row[0] == "running"
            snapshot = state.read_run_budget(run_id)
            if active_run and snapshot is not None:
                lifecycle_snapshot = dict(snapshot)
                if snapshot.get("cancel_requested"):
                    raise KeyboardInterrupt("Semantic run was cancelled")
                snapshot = state.check_run_budget(run_id)
                remaining_items = snapshot.get("remaining_items")
                if remaining_items is not None:
                    if remaining_items <= 0:
                        raise RunBudgetExceeded("items", snapshot)
                    max_items = remaining_items if max_items is None else min(max_items, remaining_items)
                deadline_ns = snapshot.get("deadline_ns")
                if deadline_ns is not None:
                    global_deadline_ns = int(deadline_ns)
                    remaining = (deadline_ns - time.time_ns()) / 1_000_000_000
                    if remaining <= 0:
                        raise RunBudgetExceeded("time", snapshot)
                    duration = remaining if duration is None else min(duration, remaining)
    # Do not ask the SemanticWorkBudget for its remaining time from its own
    # cancellation callback: ``remaining_seconds()`` invokes that callback and
    # would recurse while a Framework control read is being prepared.  This
    # local deadline is only a bounded preparation guard; the actual work budget is
    # still created below and remains authoritative for Semantic admission.
    semantic_deadline = (
        None if duration is None else time.monotonic() + float(duration)
    )
    last_check = 0.0
    cancellation: Callable[[], bool | None] | None = getattr(
        args, "_semantic_cancellation_check", None
    )

    def framework_control_checkpoint() -> None:
        """Check only external cancellation and fixed invocation deadlines."""

        if callable(cancellation) and cancellation() is True:
            raise KeyboardInterrupt("Semantic indexing was cancelled")
        if global_deadline_ns is not None and time.time_ns() >= global_deadline_ns:
            raise RunBudgetExceeded("time", lifecycle_snapshot)
        if semantic_deadline is not None and time.monotonic() >= semantic_deadline:
            raise SemanticIndexDeadlineExceeded(
                "semantic indexing exhausted its time budget"
            )

    def check_cancellation() -> bool:
        nonlocal last_check
        if callable(cancellation) and cancellation() is True:
            return True
        now = time.monotonic()
        if active_run and run_id is not None and now - last_check >= 0.1:
            # The manifest/schema was verified once above. Reinitializing the
            # whole Framework owner on every SQL progress callback is both
            # expensive and unnecessary; only the append-only cancel signal
            # can change while this Semantic stage owns the run budget.
            from neocortex.persistence.framework_connection import (
                _read_framework_cancellation_requested,
            )

            remaining: float | None = None
            if global_deadline_ns is not None:
                remaining = (global_deadline_ns - time.time_ns()) / 1_000_000_000
                if remaining <= 0:
                    raise RunBudgetExceeded("time", lifecycle_snapshot)
            if semantic_deadline is not None:
                semantic_remaining = semantic_deadline - time.monotonic()
                if semantic_remaining <= 0:
                    # Let the SemanticWorkBudget's own checkpoint report its
                    # typed deadline after this callback returns.  In
                    # particular, do not turn a Semantic-only deadline into a
                    # durable Framework cancellation.
                    return False
                remaining = (
                    semantic_remaining
                    if remaining is None
                    else min(remaining, semantic_remaining)
                )
            timeout_seconds = _INTEGRATED_FRAMEWORK_CONTROL_READ_TIMEOUT_SECONDS
            if remaining is not None:
                timeout_seconds = min(timeout_seconds, remaining)
            cancelled = _read_framework_cancellation_requested(
                args.state_directory / "framework.sqlite3",
                run_id,
                timeout_seconds=timeout_seconds,
                control_checkpoint=framework_control_checkpoint,
            )
            # Charge the complete owner-coordinated read interval to the
            # throttle.  Retaining the pre-open timestamp would immediately
            # repeat the same read on the next Semantic checkpoint.
            last_check = time.monotonic()
            if cancelled:
                return True
        return False

    return SemanticWorkBudget.from_time_budget(
        max_items=max_items,
        max_new_jobs=max_jobs,
        time_budget_seconds=duration,
        cancellation_check=check_cancellation,
        preserve_existing_generations=bool(getattr(args, "_semantic_preserve_generations", False)),
        retry_recoverable_errors=bool(getattr(args, "all", False)),
    )


def _execute_semantic_index_scopes(
    execution: _SemanticIndexExecution,
    *,
    text_operation: Callable[..., SemanticIndexResult],
    image_operation: Callable[..., SemanticIndexResult],
) -> None:
    from neocortex.semantic.semantic_config import SemanticModelUnavailableError

    for scope, operation in (
        ("text", lambda: _execute_semantic_text_index(execution, text_operation)),
        ("image", lambda: _execute_semantic_image_index(execution, image_operation)),
    ):
        try:
            operation()
        except SemanticModelUnavailableError as exc:
            # Dependency failures are typed; arbitrary source/protocol errors
            # must not be relabeled as absent models. Keep the independent
            # visual model useful if the shared text/OCR model is unavailable.
            execution.unavailable_scopes[scope] = sanitize_untrusted_text(exc, limit=800)
            emit_progress(execution.progress, ProgressEvent(
                "semantic", f"unavailable:{scope}",
                f"Modelo local no disponible para {scope}; se conservan las capacidades independientes",
                0, 1, "ámbitos", metrics=(
                    ProgressMetric("completion_status", "partial"),
                    ProgressMetric("cause", execution.unavailable_scopes[scope]),
                    ProgressMetric("next_action", "Verificar el modelo local requerido; no se descarga automáticamente"),
                ),
            ))


def _execute_semantic_text_index(
    execution: _SemanticIndexExecution,
    operation: Callable[..., SemanticIndexResult],
) -> None:
    args = execution.args
    if args.semantic_index not in {"text", "all"}:
        return
    if not execution.selected_sources:
        raise FileNotFoundError(
            "no durable PDF, DOCX, Office, audio or text cache is available"
        )
    started = time.perf_counter_ns()
    try:
        kwargs = {
            "source_kinds": execution.selected_sources,
            "model": execution.text_model,
            "model_cache": args.semantic_model_cache,
            "local_files_only": True,
            "threads": args.semantic_threads,
            "work_budget": execution.work_budget,
            "progress": execution.progress,
        }
        from neocortex.semantic.semantic_service import _writer_coordinated_scope
        with _writer_coordinated_scope(execution.writer_coordinated):
            result = operation(
                args.state_directory,
                **kwargs,
            )
    finally:
        execution.scope_timings.append(("text", time.perf_counter_ns() - started))
    _record_semantic_index_result(execution, "text", result)


def _execute_semantic_image_index(
    execution: _SemanticIndexExecution,
    operation: Callable[..., SemanticIndexResult],
) -> None:
    args = execution.args
    if args.semantic_index not in {"image", "all"} or execution.work_budget.truncated:
        return
    started = time.perf_counter_ns()
    try:
        kwargs = {
            "model_cache": args.semantic_model_cache,
            "local_files_only": True,
            "threads": args.semantic_threads,
            "embed_ocr_text": not args.semantic_no_ocr and "text" not in execution.unavailable_scopes,
            "ocr_model": execution.text_model,
            "work_budget": execution.work_budget,
            "progress": execution.progress,
        }
        from neocortex.semantic.semantic_service import _writer_coordinated_scope
        with _writer_coordinated_scope(execution.writer_coordinated):
            result = operation(
                args.state_directory,
                **kwargs,
            )
    finally:
        execution.scope_timings.append(("image", time.perf_counter_ns() - started))
    _record_semantic_index_result(execution, "image", result)


def _record_semantic_index_result(
    execution: _SemanticIndexExecution,
    scope: str,
    result: SemanticIndexResult,
) -> None:
    execution.results.append((scope, result))
    if execution.result_sink is not None:
        execution.result_sink(scope, result)


def _semantic_index_failure(
    execution: _SemanticIndexExecution,
    exc: Exception,
    *,
    print_output: bool,
) -> int:
    from neocortex.semantic.semantic_config import SemanticModelUnavailableError

    execution.args._semantic_failure = exc
    if execution.result_sink is not None:
        execution.result_sink(
            "__error__",
            {
                "schema": "neocortex.semantic-index-failure/v1",
                "error_type": type(exc).__name__,
                "error": sanitize_untrusted_text(exc, limit=800),
                "reason": sanitize_untrusted_text(getattr(exc, "reason", ""), limit=256)
                if getattr(exc, "reason", None) is not None
                else None,
            },
        )
    if print_output:
        for scope, result in execution.results:
            _print_semantic_index_result(scope, result)
            elapsed = next(
                (value for name, value in execution.scope_timings if name == scope),
                None,
            )
            if elapsed is not None:
                print(
                    f"SEMANTIC_TIMING scope={scope} elapsed_ns={elapsed} basis=monotonic_interval"
                )
    return _semantic_failure(
        "semantic-index",
        exc,
        # A Framework/SQLite or source-owner failure is not a model
        # provisioning problem.  Only the typed local-model prerequisite
        # failure may suggest an offline model snapshot.
        offline=isinstance(exc, SemanticModelUnavailableError),
        print_output=print_output,
    )


def _complete_semantic_index_execution(
    execution: _SemanticIndexExecution,
    *,
    incomplete_is_error: bool,
    print_output: bool,
) -> int:
    failed = bool(execution.unavailable_scopes)
    execution.args._semantic_scope_unavailable = dict(execution.unavailable_scopes)
    if print_output:
        for scope, reason in execution.unavailable_scopes.items():
            print(f"SEMANTIC_UNAVAILABLE scope={scope} reason={json.dumps(reason, ensure_ascii=True)}")
    for scope, result in execution.results:
        if print_output:
            _print_semantic_index_result(scope, result)
            elapsed = next(
                (value for name, value in execution.scope_timings if name == scope),
                None,
            )
            if elapsed is not None:
                print(
                    f"SEMANTIC_TIMING scope={scope} elapsed_ns={elapsed} basis=monotonic_interval"
                )
        scope_failed = _semantic_index_result_failed(
            result,
            incomplete_is_error=incomplete_is_error,
        )
        failed = failed or scope_failed
    return 2 if failed else 0


def _semantic_index_result_failed(
    result: SemanticIndexResult,
    *,
    incomplete_is_error: bool,
) -> bool:
    if not incomplete_is_error and result.truncated and result.errors == 0 and result.stale == 0:
        return False
    return not result.complete


def _publication_observation(state_directory: Path) -> dict[str, object]:
    """Observe the cross-owner gate without opening any owner database."""

    from neocortex.persistence.state_publication import (
        read_state_publication_state,
        read_state_publications,
    )

    try:
        view = read_state_publication_state(state_directory)
        publications = read_state_publications(state_directory)
    except BaseException as exc:
        return {
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {str(exc)[:512]}",
        }
    epoch = view.epoch
    publication = view.publication
    prepared = None
    duration_ns = None
    if publication is not None:
        prepared = next(
            (
                item
                for item in reversed(publications)
                if item.status == "partial"
                and item.idempotency_key == publication.idempotency_key
                and item.created_ns <= publication.created_ns
            ),
            None,
        )
        if prepared is not None:
            duration_ns = max(0, publication.created_ns - prepared.created_ns)
    return {
        "status": view.status,
        "reason": view.reason,
        "epoch": epoch.epoch,
        "event_id": epoch.event_id,
        "operation": epoch.operation,
        "owners": list(epoch.owners),
        "manifest_sha256": epoch.manifest_sha256,
        "prepared_ns": None if prepared is None else prepared.created_ns,
        "completed_ns": None if publication is None else publication.created_ns,
        "duration_ns": duration_ns,
        "timing_complete": duration_ns is not None,
    }


def _semantic_stage_for_resume(
    state_directory: Path,
    source_run_id: int,
) -> dict[str, object] | None:
    """Read one durable Semantic stage without opening any owner database.

    The Framework owner is the source of the resume boundary.  A missing
    Semantic budget is not replaced with current CLI defaults: the old run is
    not safely resumable until its exact budget is available.
    """

    from neocortex.persistence.framework_state_writer import FrameworkState

    with FrameworkState(state_directory / "framework.sqlite3", existing_only=True) as state:
        manifest = state.read_run_manifest(source_run_id)
        stages = state.read_run_stages(source_run_id)
    if manifest is None:
        raise RuntimeError(f"run {source_run_id} has no lifecycle manifest")
    semantic = next(
        (stage for stage in reversed(stages) if stage.get("stage") == "semantic"),
        None,
    )
    if semantic is None:
        return None
    status = semantic.get("status")
    if status in {"completed", "skipped"}:
        return None
    if status not in {"pending", "running", "partial", "failed", "interrupted"}:
        raise RuntimeError(f"run {source_run_id} has an unsupported Semantic stage status")
    details = semantic.get("details")
    if not isinstance(details, Mapping):
        raise RuntimeError(f"run {source_run_id} Semantic stage details are invalid")
    publication_owners = None
    if "publication_owners" in details:
        publication_owners = _stored_publication_owners(
            details.get("publication_owners"),
            label=f"run {source_run_id} Semantic publication owners",
        )
    raw_sources = details.get("selected_sources")
    selection_pending = details.get("selection_pending", False)
    if not isinstance(selection_pending, bool):
        raise RuntimeError(f"run {source_run_id} Semantic source selection is invalid")
    if selection_pending and raw_sources in (None, []):
        raw_sources = []
    if not isinstance(raw_sources, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_sources
    ):
        raise RuntimeError(f"run {source_run_id} Semantic source selection is unavailable")
    raw_budget = details.get("semantic_budget")
    if not isinstance(raw_budget, Mapping):
        raise RuntimeError(f"run {source_run_id} Semantic budget is unavailable")
    max_items = raw_budget.get("max_items")
    max_new_jobs = raw_budget.get("max_new_jobs")
    time_budget = raw_budget.get("time_budget_seconds")
    budget_version = details.get("semantic_budget_version", 1)
    if budget_version not in {1, 2} or type(budget_version) is not int:
        raise RuntimeError(f"run {source_run_id} Semantic budget version is unsupported")
    if any(
        not (budget_version == 2 and value is None)
        and (type(value) is not int or value < 1)
        for value in (max_items, max_new_jobs)
    ):
        raise RuntimeError(f"run {source_run_id} Semantic budget is invalid")
    if time_budget is None and budget_version == 2:
        time_budget_value = None
    elif isinstance(time_budget, bool) or not isinstance(time_budget, (int, float)):
        raise RuntimeError(f"run {source_run_id} Semantic budget is invalid")
    else:
        time_budget_value = float(time_budget)
        if not 0.001 <= time_budget_value <= 172_800.0:
            raise RuntimeError(f"run {source_run_id} Semantic budget is invalid")
    image_available = details.get("image_available", False)
    if not isinstance(image_available, bool):
        raise RuntimeError(f"run {source_run_id} Semantic image availability is invalid")
    return {
        "status": status,
        "manifest": manifest,
        "details": details,
        "selected_sources": tuple(dict.fromkeys(raw_sources)),
        "selection_pending": selection_pending,
        "image_available": image_available,
        "max_items": max_items,
        "max_new_jobs": max_new_jobs,
        "time_budget_seconds": time_budget_value,
        "publication_owners": publication_owners,
    }


def semantic_resume_available(args: argparse.Namespace, source_run_id: int) -> bool:
    """Return whether a previous ``--all`` Semantic stage needs resumption."""

    try:
        return _semantic_stage_for_resume(args.state_directory, source_run_id) is not None
    except (OSError, RuntimeError, ValueError):
        # Let the actual resume path report the durable reason and exit code.
        return True


def _semantic_resume_args(
    args: argparse.Namespace,
    source_run_id: int,
) -> argparse.Namespace | None:
    spec = _semantic_stage_for_resume(args.state_directory, source_run_id)
    if spec is None:
        return None
    effective = argparse.Namespace(**vars(args))
    effective.all = True
    selected_sources = spec.get("selected_sources")
    selection_pending = spec.get("selection_pending", False)
    image_available = spec.get("image_available")
    max_items = spec.get("max_items")
    max_new_jobs = spec.get("max_new_jobs")
    time_budget_seconds = spec.get("time_budget_seconds")
    publication_owners = spec.get("publication_owners")
    details = spec.get("details")
    if (
        not isinstance(selected_sources, tuple)
        or any(not isinstance(value, str) for value in selected_sources)
        or not isinstance(selection_pending, bool)
        or not isinstance(image_available, bool)
        or (max_items is not None and type(max_items) is not int)
        or (max_new_jobs is not None and type(max_new_jobs) is not int)
        or (time_budget_seconds is not None and (
            isinstance(time_budget_seconds, bool) or not isinstance(time_budget_seconds, (int, float))
        ))
        or (publication_owners is not None and (
            not isinstance(publication_owners, tuple)
            or any(not isinstance(value, str) for value in publication_owners)
        ))
        or not isinstance(details, Mapping)
    ):
        raise RuntimeError(f"run {source_run_id} Semantic resume specification is invalid")
    effective.semantic_source = None if selection_pending else list(selected_sources)
    effective.semantic_max_items = max_items
    effective.semantic_max_new_jobs = max_new_jobs
    effective.semantic_time_budget_seconds = None if time_budget_seconds is None else float(time_budget_seconds)
    effective.semantic_index = (
        "all" if selected_sources and image_available else "text" if selected_sources else "image"
    )
    effective._semantic_selection_pending = selection_pending
    for name, default in (
        ("semantic_text_profile", "quality"),
        ("semantic_threads", None),
        ("semantic_no_ocr", False),
    ):
        value = details.get(name, default)
        if name == "semantic_text_profile" and value not in {"quality", "compact"}:
            raise RuntimeError(f"run {source_run_id} Semantic {name} is invalid")
        if (
            name == "semantic_threads"
            and value is not None
            and (type(value) is not int or value < 1)
        ):
            raise RuntimeError(f"run {source_run_id} Semantic {name} is invalid")
        if name == "semantic_no_ocr" and not isinstance(value, bool):
            raise RuntimeError(f"run {source_run_id} Semantic {name} is invalid")
        setattr(effective, name, value)
    raw_cache = details.get("semantic_model_cache")
    if raw_cache is not None and not isinstance(raw_cache, str):
        raise RuntimeError(f"run {source_run_id} Semantic model cache is invalid")
    effective.semantic_model_cache = None if raw_cache is None else Path(raw_cache)
    effective._semantic_resume_source_run_id = source_run_id
    effective._semantic_resume_image_available = image_available
    effective._semantic_complete_all = bool(details.get("complete_all", False))
    if publication_owners is None:
        effective._semantic_publication_owners = None
    else:
        effective._semantic_publication_owners = publication_owners
    return effective


_INTEGRATED_PUBLICATION_OWNER_ORDER = ("semantic",)


def _stored_publication_owners(
    raw: object,
    *,
    label: str,
) -> tuple[str, ...]:
    """Validate the additive owner scope carried by a Semantic stage."""

    if not isinstance(raw, (list, tuple)) or not raw:
        raise RuntimeError(f"{label} is invalid")
    owners = tuple(raw)
    if (
        any(
            not isinstance(owner, str)
            or owner not in _INTEGRATED_PUBLICATION_OWNER_ORDER
            for owner in owners
        )
        or len(set(owners)) != len(owners)
        or "semantic" not in owners
    ):
        raise RuntimeError(f"{label} is invalid")
    return tuple(owner for owner in _INTEGRATED_PUBLICATION_OWNER_ORDER if owner in owners)


def _integrated_publication_owners(
    args: argparse.Namespace,
    selected_sources: tuple[str, ...],
) -> tuple[str, ...]:
    """Keep an explicit checkpoint scope while retaining the legacy fallback."""

    raw = getattr(args, "_semantic_publication_owners", None)
    if raw is None:
        owners = {"semantic"}
    else:
        owners = set(_stored_publication_owners(raw, label="Semantic publication owners"))
    return tuple(owner for owner in _INTEGRATED_PUBLICATION_OWNER_ORDER if owner in owners)


def _validate_integrated_manifest_root(
    args: argparse.Namespace,
    manifest: Mapping[str, object],
    controls: _IntegratedStartReadBudget,
) -> None:
    """Revalidate the producer root identity without opening any SQLite owner."""

    root_value = manifest.get("root")
    root_identity = manifest.get("root_identity")
    if (
        not isinstance(root_value, str)
        or not root_value
        or not isinstance(root_identity, list)
        or len(root_identity) != 3
        or any(type(value) is not int for value in root_identity)
    ):
        raise StatePublicationRecoveryRequired("original Framework root identity is invalid")
    try:
        root = Path(root_value)
        requested_root = getattr(args, "root", root)
        if requested_root is not None and Path(requested_root).resolve() != root.resolve():
            raise StatePublicationRecoveryRequired(
                "original Semantic recovery belongs to another corpus root"
            )
        metadata = root.stat()
    except StatePublicationRecoveryRequired:
        raise
    except (OSError, RuntimeError) as exc:
        raise StatePublicationRecoveryRequired("original corpus root is unavailable") from exc
    observed_identity = (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(stat_birthtime_ns(metadata)),
    )
    if list(observed_identity) != root_identity:
        raise StatePublicationRecoveryRequired("original corpus root identity changed")
    controls.check()


def _read_pending_integrated_metadata(
    args: argparse.Namespace,
    controls: _IntegratedStartReadBudget,
) -> _PendingIntegratedMetadata | None:
    """Read the authenticated old manifest without rehydrating its producer."""

    from neocortex.persistence.state_publication import read_state_publication_state
    from neocortex.runtime.orchestration.run_manifest import verify_event_payload

    controls.check()
    view = read_state_publication_state(args.state_directory)
    controls.check()
    if view.status in {"absent", "complete"}:
        return None
    if view.status != "blocked":
        raise StatePublicationRecoveryRequired(view.reason or "state publication is not complete")
    pending_rows = tuple(getattr(view, "pending", ()))
    if not pending_rows:
        # Legacy/read-only test views may expose only status+epoch. Treat a
        # blocked marker without an authenticated pending row as the existing
        # safe fresh-start shortcut rather than inventing a resume producer.
        return None
    if len(pending_rows) != 1:
        raise StatePublicationRecoveryRequired("pending publication is ambiguous")
    pending = pending_rows[0]
    if pending.operation != "framework-all-semantic":
        raise StatePublicationRecoveryRequired("pending publication is not an integrated Semantic run")
    if pending.epoch != view.epoch.epoch:
        raise StatePublicationRecoveryRequired("pending publication epoch is detached")
    allowed_owners = {"semantic"}
    if not pending.owners or any(owner not in allowed_owners for owner in pending.owners):
        raise StatePublicationRecoveryRequired("pending publication owner scope is unavailable")
    previous_owners = set(view.epoch.owners)
    if view.publication is not None:
        previous_owners.update(view.publication.owners)
    historical_head_owners = {
        head.owner
        for head in (
            *pending.owner_heads,
            *view.epoch.owner_heads,
            *(() if view.publication is None else view.publication.owner_heads),
        )
    }
    if any(owner not in allowed_owners for owner in historical_head_owners):
        raise StatePublicationRecoveryRequired("historical owner-head scope is unavailable")
    previous_owners.update(historical_head_owners)
    if any(owner not in allowed_owners for owner in previous_owners):
        raise StatePublicationRecoveryRequired("previous publication owner scope is unavailable")

    if pending.manifest_sha256 is None:
        if view.epoch.epoch != 0 or view.publication is not None or pending.owner_heads:
            raise StatePublicationRecoveryRequired("original publication manifest is unavailable")
        return _PendingIntegratedMetadata(
            event_id=pending.event_id,
            expected_epoch=view.epoch.epoch,
            pending_owners=tuple(pending.owners),
            previous_owners=tuple(sorted(previous_owners)),
            manifest=None,
        )

    manifest_sha256 = pending.manifest_sha256
    database = args.state_directory / "framework.sqlite3"
    try:
        with _bounded_framework_metadata_read(database, controls) as connection:
            rows = connection.execute(
                """SELECT run_id,details_json FROM run_events
                WHERE phase='lifecycle-manifest' AND message='Run manifest published'
                AND json_valid(details_json)
                AND json_extract(details_json,'$.digest')=? LIMIT 2""",
                ("sha256:" + manifest_sha256,),
            ).fetchall()
            controls.check()
            if len(rows) != 1:
                raise StatePublicationRecoveryRequired(
                    "original Framework manifest is absent or ambiguous"
                )
            run_id = int(rows[0]["run_id"])
            manifest = verify_event_payload(json.loads(rows[0]["details_json"]))
            if manifest.get("run_id") != run_id or manifest.get("digest") != (
                "sha256:" + manifest_sha256
            ):
                raise StatePublicationRecoveryRequired("original Framework manifest is detached")
    except StatePublicationRecoveryRequired:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise StatePublicationRecoveryRequired(
            f"original Semantic contract cannot be read: {type(exc).__name__}: {exc}"
        ) from exc

    _validate_integrated_manifest_root(args, manifest, controls)
    return _PendingIntegratedMetadata(
        event_id=pending.event_id,
        expected_epoch=view.epoch.epoch,
        pending_owners=tuple(pending.owners),
        previous_owners=tuple(sorted(previous_owners)),
        manifest=manifest,
    )


def _observe_fresh_integrated_heads(
    state_directory: Path,
    *,
    controls: _IntegratedStartReadBudget,
    writer_connection: _SemanticOwnerLease | None = None,
) -> tuple[StateOwnerHead, ...]:
    """Observe the Semantic head once through the bounded read fence."""
    from neocortex.semantic.semantic_publication_heads import (
        observe_integrated_owner_heads,
    )
    snapshot_budget = controls.snapshot_budget()
    if writer_connection is None:
        return observe_integrated_owner_heads(
            state_directory,
            snapshot_budget=snapshot_budget,
            deadline_monotonic=controls.deadline,
            cancellation_check=controls._snapshot_checkpoint,
        )
    return observe_integrated_owner_heads(
        state_directory,
        snapshot_budget=snapshot_budget,
        deadline_monotonic=controls.deadline,
        cancellation_check=controls._snapshot_checkpoint,
        _writer_lease=writer_connection,
    )


def _fresh_integrated_checkpoint(
    args: argparse.Namespace,
    *,
    controls: _IntegratedStartReadBudget,
    print_output: bool,
    pre_checkpoint_hook: Callable[..., object] | None = None,
) -> None:
    """Restart a stale integrated marker without resuming its Semantic producer."""

    from neocortex.runtime.control.locking import FrameworkRunLock
    from neocortex.persistence.state_publication import read_state_publication_state

    try:
        args.state_directory.lstat()
    except FileNotFoundError:
        return
    controls.check()
    initial_view = read_state_publication_state(args.state_directory)
    if initial_view.status in {"absent", "complete"}:
        if initial_view.epoch.owners == ("semantic",):
            args._semantic_publication_owners = initial_view.epoch.owners
        controls.apply_elapsed_to_explicit_caps(args)
        return
    # This path can now write a lock, the publication journal, or Framework
    # metadata before the normal orchestrator.
    from neocortex.integrations.inventory.inventory_boundary import state_sqlite_mutation_paths

    _validate_semantic_state_write(
        args.state_directory,
        database=True,
        extra_paths=(
            *state_sqlite_mutation_paths(args.state_directory / "framework.sqlite3"),
            args.state_directory / "state-publication.lock",
            args.state_directory / "state-publication-journal.jsonl",
            args.state_directory / "state-epoch.json",
        ),
    )
    with FrameworkRunLock(args.state_directory / "framework.lock"):
        controls.check()
        current_view = read_state_publication_state(args.state_directory)
        current_owners = current_view.epoch.owners
        if (
            current_view.status == "complete"
            and "semantic" in current_owners
            and current_owners == ("semantic",)
        ):
            # Preserve the published Semantic owner even when the current run
            # has no input candidates.
            args._semantic_publication_owners = current_owners
        metadata = _read_pending_integrated_metadata(args, controls)
        if metadata is None:
            controls.apply_elapsed_to_explicit_caps(args)
            return
        if metadata.manifest is None:
            _recover_pending_integrated_publication(args.state_directory)
            controls.apply_elapsed_to_explicit_caps(args)
            return
        manifest = metadata.manifest

        if pre_checkpoint_hook is not None:
            pre_checkpoint_hook(args, metadata, controls)
            controls.check()
        from neocortex.semantic.semantic_publication_heads import _semantic_owner_lease

        def observe_fresh_heads() -> tuple[StateOwnerHead, ...]:
            with _semantic_owner_lease(
                args.state_directory,
                checkpoint=controls.check,
                timeout_seconds=controls.remaining_seconds(),
            ) as owner:
                return _observe_fresh_integrated_heads(
                    args.state_directory,
                    controls=controls,
                    writer_connection=owner,
                )

        initial_heads = observe_fresh_heads()
        controls.check()
        args._semantic_publication_owners = ("semantic",)

        from neocortex.persistence.state_publication import restart_state_publication_checkpoint

        def verify_owner_heads() -> tuple[StateOwnerHead, ...]:
            _validate_integrated_manifest_root(args, manifest, controls)
            # Repair only once, before sealing the checkpoint. A verifier at
            # the commit boundary observes drift; it must never repair it.
            return observe_fresh_heads()

        checkpoint = restart_state_publication_checkpoint(
            args.state_directory,
            event_id=metadata.event_id,
            expected_epoch=metadata.expected_epoch,
            owner_heads=initial_heads,
            verify_owner_heads=verify_owner_heads,
        )
        args._semantic_publication_owners = _stored_publication_owners(
            checkpoint.owners,
            label="fresh Semantic checkpoint owners",
        )
        controls.check()
        if print_output and not bool(getattr(args, "json_output", False)):
            _print_console_line(
                "SEMANTIC_CHECKPOINT status=restarted "
                f"epoch={checkpoint.epoch}"
            )
        controls.apply_elapsed_to_explicit_caps(args)


def prepare_integrated_semantic_start(
    args: argparse.Namespace,
    *,
    progress: ProgressCallback | None = None,
    print_output: bool = True,
    cancellation_check: Callable[[], bool | None] | None = None,
    metadata_timeout_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    pre_checkpoint_hook: Callable[..., object] | None = None,
) -> int:
    """Prepare a fresh integrated start or preserve strict explicit resume."""

    if getattr(args, "resume_run", None) is not None:
        return recover_pending_integrated_semantic(
            args,
            progress=progress,
            print_output=print_output,
        )
    if not bool(getattr(args, "all", False)):
        return 0
    if pre_checkpoint_hook is None:
        candidate_hook = getattr(args, "_semantic_pre_checkpoint_hook", None)
        if candidate_hook is not None and not callable(candidate_hook):
            raise TypeError("Semantic pre-checkpoint hook must be callable")
        pre_checkpoint_hook = candidate_hook
    controls = _IntegratedStartReadBudget(
        args,
        cancellation_check=(
            cancellation_check
            if cancellation_check is not None
            else getattr(args, "_semantic_cancellation_check", None)
        ),
        clock=clock,
        metadata_timeout_seconds=metadata_timeout_seconds,
    )
    from neocortex.semantic.semantic_publication_heads import PublicationHeadsError

    try:
        _fresh_integrated_checkpoint(
            args,
            controls=controls,
            print_output=print_output,
            pre_checkpoint_hook=pre_checkpoint_hook,
        )
    except PublicationHeadsError as exc:
        raise StatePublicationRecoveryRequired(str(exc)) from exc
    return 0


def _pending_integrated_source_run(state_directory: Path) -> int | None:
    """Find one original producer by authenticated manifest and stage data."""

    from neocortex.persistence.sqlite_immutable import SQLiteReadSession, preferred_sqlite_read_mode
    from neocortex.persistence.state_publication import (
        publication_idempotency_key,
        read_state_publication_state,
        resume_state_publication,
    )
    from neocortex.runtime.orchestration.run_manifest import RUN_STAGE_SCHEMA, verify_event_payload

    view = read_state_publication_state(state_directory)
    if view.status in {"absent", "complete"}:
        return None
    if (
        len(view.pending) != 1
        or view.pending[0].operation != "framework-all-semantic"
        or view.pending[0].manifest_sha256 is None
    ):
        raise StatePublicationRecoveryRequired("pending publication has no unique Semantic producer")
    pending = view.pending[0]
    manifest_sha256 = pending.manifest_sha256
    if manifest_sha256 is None:
        raise StatePublicationRecoveryRequired("original publication manifest is unavailable")
    database = state_directory / "framework.sqlite3"
    if not database.is_file():
        raise StatePublicationRecoveryRequired("original Framework manifest is unavailable")
    try:
        with SQLiteReadSession(database, mode=preferred_sqlite_read_mode(database)) as connection:
            started = time.monotonic()
            connection.set_progress_handler(lambda: int(time.monotonic() - started > 10.0), 10_000)
            rows = connection.execute(
                """SELECT run_id,details_json FROM run_events
                WHERE phase='lifecycle-manifest' AND message='Run manifest published'
                AND json_valid(details_json)
                AND json_extract(details_json,'$.digest')=? LIMIT 2""",
                ("sha256:" + manifest_sha256,),
            ).fetchall()
            if len(rows) != 1:
                raise StatePublicationRecoveryRequired("original Framework manifest is absent or ambiguous")
            run_id = int(rows[0]["run_id"])
            manifest = verify_event_payload(json.loads(rows[0]["details_json"]))
            if manifest.get("run_id") != run_id:
                raise StatePublicationRecoveryRequired("original Framework manifest has a different run")
            stages = connection.execute(
                """SELECT details_json FROM run_events WHERE run_id=?
                AND phase='lifecycle-stage' AND message='Lifecycle stage transitioned'
                ORDER BY event_id DESC LIMIT 65""",
                (run_id,),
            ).fetchall()
            if len(stages) > 64:
                raise StatePublicationRecoveryRequired("original Semantic stage exceeds its bound")
            semantic = None
            for row in stages:
                stage = json.loads(row["details_json"])
                if (
                    not isinstance(stage, dict)
                    or stage.get("schema") != RUN_STAGE_SCHEMA
                    or stage.get("manifest_digest") != manifest["digest"]
                    or stage.get("run_id") != run_id
                ):
                    raise StatePublicationRecoveryRequired("original Semantic stage is detached from its manifest")
                if stage.get("stage") == "semantic":
                    semantic = stage
                    break
            if semantic is None or semantic.get("status") in {"completed", "skipped"}:
                raise StatePublicationRecoveryRequired("original Semantic producer is not resumable")
            details = semantic.get("details")
            if not isinstance(details, dict):
                raise StatePublicationRecoveryRequired("original Semantic stage details are invalid")
            sources = details.get("selected_sources")
            images = details.get("image_available")
            if (
                not isinstance(sources, list)
                or any(not isinstance(source, str) or not source for source in sources)
                or len(sources) != len(set(sources))
                or not isinstance(images, bool)
            ):
                raise StatePublicationRecoveryRequired("original Semantic source selection is unavailable")
            if "publication_owners" in details:
                try:
                    publication_owners = _stored_publication_owners(
                        details.get("publication_owners"),
                        label="original Semantic publication owners",
                    )
                except RuntimeError as exc:
                    raise StatePublicationRecoveryRequired(str(exc)) from exc
            else:
                publication_owners = None
        # The original raw key, not the already-hashed journal key, binds the
        # producer. This is a metadata-only transaction rehydration, not abort.
        resume_state_publication(
            state_directory,
            event_id=pending.event_id,
            operation="framework-all-semantic",
            owners=publication_owners or ("semantic",),
            idempotency_key=publication_idempotency_key(
                "framework-all-semantic", run_id, tuple(sources), images
            ),
            manifest_sha256=manifest_sha256,
            expected_epoch=view.epoch.epoch,
        )
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise StatePublicationRecoveryRequired(f"original Semantic contract cannot be read: {exc}") from exc
    return run_id


def recover_pending_integrated_semantic(
    args: argparse.Namespace,
    *,
    progress: ProgressCallback | None = None,
    print_output: bool = True,
) -> int:
    """Recover the original producer, with typed failures for unavailable inputs."""

    try:
        return _recover_pending_integrated_semantic(
            args, progress=progress, print_output=print_output
        )
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise StatePublicationRecoveryRequired(
            f"original recovery input is unavailable or incompatible: {type(exc).__name__}: {exc}"
        ) from exc


def _recover_pending_integrated_semantic(
    args: argparse.Namespace,
    *,
    progress: ProgressCallback | None = None,
    print_output: bool = True,
) -> int:
    """Roll the original producer forward before starting another inventory.

    A pending marker stays durable throughout resumption. In particular, this
    path never claims that a modern head vector proves a legacy rollback.
    """

    from neocortex.persistence.state_publication import read_state_publication_state
    from neocortex.runtime.control.locking import FrameworkRunLock
    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.runtime.models import FrameworkConfig
    from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
    from neocortex.runtime.orchestration.run_lifecycle import RunHeartbeat
    from neocortex.runtime.orchestration.run_manifest import RunManifest
    from neocortex.platform.policy import stat_birthtime_ns

    try:
        args.state_directory.lstat()
    except FileNotFoundError:
        # A first invocation has no publication to recover. Do not create a
        # state owner merely to perform the preflight, or confuse missing state
        # with a malformed existing publication directory.
        return 0
    initial = read_state_publication_state(args.state_directory)
    if initial.status in {"absent", "complete"}:
        return 0
    recovery_started = time.monotonic()
    with FrameworkRunLock(args.state_directory / "framework.lock"):
        current = read_state_publication_state(args.state_directory)
        if (
            current.epoch.epoch == 0 and current.publication is None
            and len(current.pending) == 1
            and current.pending[0].operation == "framework-all-semantic"
            and current.pending[0].manifest_sha256 is None
            and not current.pending[0].owner_heads
        ):
            # Preserve the established epoch-zero compatibility path only
            # for a genuinely unbound initial marker. Later epochs always
            # require the exact producer and cannot take this shortcut.
            _recover_pending_integrated_publication(args.state_directory)
            return 0
        source_run_id = _pending_integrated_source_run(args.state_directory)
        if source_run_id is None:
            return 0
        requested_run = getattr(args, "resume_run", None)
        policy_run_id = source_run_id if requested_run is None else requested_run
        with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
            source_manifest = state.read_run_manifest(source_run_id)
            policy_manifest = state.read_run_manifest(policy_run_id)
            if source_manifest is None or policy_manifest is None:
                raise StatePublicationRecoveryRequired("original recovery manifest is unavailable")
            if policy_run_id != source_run_id:
                parent = policy_manifest
                for _ in range(16):
                    if any(parent.get(key) != source_manifest.get(key) for key in ("root", "root_identity")):
                        raise StatePublicationRecoveryRequired("recovery lineage belongs to a different corpus")
                    parent_id = parent.get("source_run_id")
                    if parent_id == source_run_id:
                        break
                    if type(parent_id) is not int:
                        raise StatePublicationRecoveryRequired("another run owns the pending Semantic publication")
                    next_parent = state.read_run_manifest(parent_id)
                    if next_parent is None:
                        raise StatePublicationRecoveryRequired("recovery lineage is incomplete")
                    parent = next_parent
                else:
                    raise StatePublicationRecoveryRequired("recovery lineage exceeds its bound")
            root = Path(source_manifest["root"])
            requested_root = getattr(args, "root", root)
            if requested_root is not None and Path(requested_root).resolve() != root.resolve():
                raise StatePublicationRecoveryRequired("pending Semantic recovery belongs to a different corpus root")
            root_stat = root.stat()
            identity = (root_stat.st_dev, root_stat.st_ino, stat_birthtime_ns(root_stat))
            if list(identity) != source_manifest["root_identity"]:
                raise StatePublicationRecoveryRequired("original corpus root identity changed")
            # The outer FrameworkRunLock has excluded the former writer. Use
            # the existing recovery transition before beginning its successor,
            # including a process that died before marking its run terminal.
            state.mark_abandoned_runs()
            budget_owner = FrameworkOrchestrator(
                FrameworkConfig(
                    root=root,
                    state_directory=args.state_directory,
                    route_only=True,
                    resume_run_id=policy_run_id,
                    run_max_items=getattr(args, "run_max_items", None),
                    run_max_bytes=getattr(args, "run_max_bytes", None),
                    run_time_budget_seconds=getattr(args, "run_time_budget_seconds", None),
                )
            )
            recovery_budget, _ = budget_owner._route_only_budget(state, policy_run_id)
            recovery_run_id = state.begin_operational_run(
                root, run_kind="resume", source_run_id=policy_run_id
            )
            manifest = RunManifest(
                run_id=recovery_run_id,
                run_kind="resume",
                root=str(root),
                root_identity=identity,
                selected_routes=(),
                source_run_id=policy_run_id,
                configuration={
                    "operation": "framework-all-semantic-recovery",
                    "publication_source_run_id": source_run_id,
                    "source_manifest_digest": source_manifest["digest"],
                },
                budget=recovery_budget.payload(),
                input_snapshot=source_manifest.get("input_snapshot", {}),
            )
            try:
                state.publish_run_manifest(recovery_run_id, manifest.event_payload())
            except BaseException as exc:
                state.abort_run_start(recovery_run_id, exc, cancelled=False)
                raise
        effective = argparse.Namespace(**vars(args))
        effective._semantic_publication_source_run_id = source_run_id
        effective._semantic_preserve_generations = True
        if print_output:
            _print_console_line(f"SEMANTIC_RECOVERY run_id={recovery_run_id} source_run_id={source_run_id} status=starting")
        try:
            with RunHeartbeat(args.state_directory / "framework.sqlite3", recovery_run_id):
                result = run_integrated_all_semantic_index(
                    effective,
                    progress=progress,
                    print_output=print_output,
                    run_id=recovery_run_id,
                    resume_source_run_id=source_run_id,
                    framework_lock_held=True,
                )
                final = read_state_publication_state(args.state_directory)
                if result != 0 or final.status != "complete":
                    raise StatePublicationRecoveryRequired("original Semantic producer remains incomplete; progress was preserved")
                with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
                    state.complete_operational_run(recovery_run_id)
        except BaseException as exc:
            with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
                if isinstance(exc, KeyboardInterrupt):
                    state.request_run_cancellation(recovery_run_id, "user")
                    state.cancel_initial_run(recovery_run_id)
                else:
                    state.fail_initial_run(recovery_run_id)
            raise
        with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
            state.record_event(
                source_run_id,
                "info",
                "semantic-recovery",
                "Pending Semantic publication completed",
                {
                    "recovery_run_id": recovery_run_id,
                    "previous_epoch": initial.epoch.epoch,
                    "published_epoch": final.epoch.epoch,
                },
            )
            consumed = state.read_run_budget(recovery_run_id)
        if consumed is not None:
            if getattr(args, "run_max_items", None) is not None:
                args.run_max_items = max(0, args.run_max_items - consumed["consumed_items"])
            if getattr(args, "run_max_bytes", None) is not None:
                args.run_max_bytes = max(0, args.run_max_bytes - consumed["consumed_bytes"])
            if getattr(args, "run_time_budget_seconds", None) is not None:
                remaining_duration = args.run_time_budget_seconds - (time.monotonic() - recovery_started)
                if remaining_duration <= 0:
                    from neocortex.persistence.framework_state_writer import RunBudgetExceeded

                    raise RunBudgetExceeded("time", consumed)
                args.run_time_budget_seconds = remaining_duration
        if print_output:
            _print_console_line(f"SEMANTIC_RECOVERY source_run_id={source_run_id} status=completed epoch={final.epoch.epoch}")
        return 0


def _recover_pending_integrated_publication(state_directory: Path) -> bool:
    """Resolve an old prepare only when the publication API proves its scope."""

    from neocortex.persistence.state_publication import (
        abort_state_publication,
        abort_unbound_state_publication,
        canonical_owner_heads,
        read_state_publication_state,
    )

    view = read_state_publication_state(state_directory)
    if view.status != "blocked":
        return False
    pending = tuple(item for item in view.pending if item.operation == "framework-all-semantic")
    if len(pending) != 1 or len(pending) != len(view.pending):
        raise StatePublicationRecoveryRequired("another publication is pending")
    prepared = pending[0]
    if prepared.owner_heads:
        observed = _observe_integrated_heads(state_directory)
        if canonical_owner_heads(observed) != canonical_owner_heads(prepared.owner_heads):
            raise StatePublicationRecoveryRequired("owner-head drift")
        abort_state_publication(
            state_directory,
            event_id=prepared.event_id,
            observed_owner_heads=observed,
            expected_epoch=view.epoch.epoch,
            detail="Semantic resume verified unchanged owner heads before abort",
        )
        return True
    if view.epoch.epoch != 0 or view.publication is not None:
        raise StatePublicationRecoveryRequired(
            "unbound prepare requires resuming its original producer"
        )
    abort_unbound_state_publication(
        state_directory,
        event_id=prepared.event_id,
        expected_epoch=0,
        detail="Semantic resume invalidated unbound initial prepare",
    )
    return True


def _integrated_stage_details(
    args: argparse.Namespace,
    *,
    selected_sources: tuple[str, ...],
    image_available: bool,
    semantic_exit_code: int | None = None,
    error: BaseException | None = None,
    recovery_required: bool = False,
    resume_source_run_id: int | None = None,
) -> dict[str, object]:
    """Build a bounded link from the Framework run to Semantic owners."""

    from neocortex.semantic.semantic_service import SEMANTIC_DATABASE_NAME

    details: dict[str, object] = {
        "selected_sources": list(selected_sources[:32]),
        "selection_pending": False,
        "complete_all": bool(getattr(args, "_semantic_complete_all", False)),
        "semantic_budget_version": 2,
        "source_unavailable": dict(getattr(args, "_semantic_source_unavailable", {})),
        "model_unavailable": dict(getattr(args, "_semantic_scope_unavailable", {})),
        "source_without_content": list(getattr(args, "_semantic_source_empty", ())),
        "image_available": image_available,
        "semantic_budget": {
            "max_items": getattr(args, "semantic_max_items", None),
            "max_new_jobs": getattr(args, "semantic_max_new_jobs", None),
            "time_budget_seconds": getattr(args, "semantic_time_budget_seconds", None),
        },
        "semantic_text_profile": getattr(args, "semantic_text_profile", "quality"),
        "semantic_model_cache": (
            None
            if getattr(args, "semantic_model_cache", None) is None
            else str(args.semantic_model_cache)
        ),
        "semantic_threads": getattr(args, "semantic_threads", None),
        "semantic_no_ocr": bool(getattr(args, "semantic_no_ocr", False)),
        "semantic_database": str(args.state_directory / SEMANTIC_DATABASE_NAME),
        "publication": _publication_observation(args.state_directory),
        "recovery_required": recovery_required,
    }
    publication_owners = getattr(args, "_semantic_publication_owners", None)
    if publication_owners is not None:
        details["publication_owners"] = list(
            _stored_publication_owners(
                publication_owners,
                label="Semantic publication owners",
            )
        )
    if semantic_exit_code is not None:
        details["semantic_exit_code"] = semantic_exit_code
    if error is not None:
        details["error_type"] = type(error).__name__
        details["error"] = str(error)[:512]
    if resume_source_run_id is not None:
        details["resume_source_run_id"] = resume_source_run_id
    return details


def _record_integrated_semantic_stage(
    args: argparse.Namespace,
    run_id: int | None,
    status: str,
    *,
    details: dict[str, object],
    idempotency_key: str,
) -> None:
    """Persist Semantic lifecycle metadata in the Framework owner."""

    if run_id is None:
        return
    from neocortex.persistence.framework_state_writer import FrameworkState

    with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
        state.publish_run_stage(
            run_id,
            "semantic",
            status,
            details=details,
            idempotency_key=idempotency_key,
        )


def _begin_integrated_publication(
    args: argparse.Namespace,
    run_id: int | None,
    *,
    selected_sources: tuple[str, ...],
    image_available: bool,
):
    """Prepare the logical Semantic publication gate for an ``--all`` run."""

    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.persistence.state_publication import (
        begin_state_publication,
        publication_idempotency_key,
        read_state_publication_state,
        resume_state_publication,
    )

    view = read_state_publication_state(args.state_directory)
    source_run_id = getattr(
        args, "_semantic_publication_source_run_id", getattr(args, "_semantic_resume_source_run_id", None)
    )
    owners = _integrated_publication_owners(args, selected_sources)
    args._semantic_publication_owners = owners
    if view.status == "blocked" and type(source_run_id) is int:
        with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
            source_manifest = state.read_run_manifest(source_run_id)
        if source_manifest is None or len(view.pending) != 1:
            raise StatePublicationRecoveryRequired("original Semantic producer is unavailable")
        return resume_state_publication(
            args.state_directory,
            event_id=view.pending[0].event_id,
            operation="framework-all-semantic",
            owners=owners,
            idempotency_key=publication_idempotency_key(
                "framework-all-semantic", source_run_id, selected_sources, image_available
            ),
            manifest_sha256=str(source_manifest["digest"])[len("sha256:"):],
            expected_epoch=view.epoch.epoch,
        )
    _recover_pending_integrated_publication(args.state_directory)
    if run_id is None:
        return None
    with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
        manifest = state.read_run_manifest(run_id)
    if manifest is None:
        raise RuntimeError(f"run {run_id} has no manifest for Semantic publication")
    key = publication_idempotency_key(
        "framework-all-semantic",
        run_id,
        selected_sources,
        image_available,
    )
    view = read_state_publication_state(args.state_directory)
    if view.status not in {"absent", "complete"}:
        raise StatePublicationRecoveryRequired("state publication is not ready")
    baseline_heads = _observe_integrated_heads(
        args.state_directory,
        work_budget=getattr(args, "_semantic_work_budget", None),
    )
    previous = {head.owner: head for head in view.epoch.owner_heads}
    if any(
        head.revision == 0 and head.owner in previous and previous[head.owner].revision > 0
        for head in baseline_heads
    ):
        raise StatePublicationRecoveryRequired("a previously published owner is now absent or empty")
    return begin_state_publication(
        args.state_directory,
        operation="framework-all-semantic",
        owners=owners,
        idempotency_key=key,
        manifest_sha256=str(manifest["digest"])[len("sha256:") :],
        detail="Semantic owner work is pending its terminal lifecycle publication",
        owner_heads=baseline_heads,
        expected_epoch=view.epoch.epoch,
    )


def _final_publication_owner_heads(
    args: argparse.Namespace,
    captured_results: list[tuple[str, object]],
    *,
    selected_sources: tuple[str, ...],
):
    """Derive bounded owner-head identities from published Semantic results."""

    from neocortex.semantic.semantic_publication_heads import (
        PublicationHeadsError,
        observe_semantic_generation_heads,
    )

    generations: dict[str, int] = {}
    for _scope, value in captured_results:
        if getattr(value, "complete", False) is not True:
            raise RuntimeError("Semantic publication requires complete generation results")
        for generation in getattr(value, "generations", ()):
            summary = getattr(generation, "summary", None)
            generation_id = getattr(summary, "generation_id", None)
            model_signature = getattr(summary, "model_signature", None)
            if (
                isinstance(generation_id, int)
                and isinstance(model_signature, str)
                and getattr(summary, "status", None) == "ready"
                and getattr(summary, "unfinished", 0) == 0
                and getattr(summary, "errors", 0) == 0
                and getattr(summary, "stale", 0) == 0
            ):
                generations[model_signature] = generation_id
    if not generations:
        raise RuntimeError("Semantic publication produced no owner generation")
    try:
        budget = getattr(args, "_semantic_work_budget", None)
        remaining = None if budget is None else budget.remaining_seconds()
        from neocortex.semantic.semantic_publication_heads import _semantic_owner_lease

        with _semantic_owner_lease(
            args.state_directory,
            checkpoint=None if budget is None else budget.checkpoint,
            timeout_seconds=60.0 if remaining is None else max(0.001, remaining),
        ) as owner:
            observed_generations = dict(observe_semantic_generation_heads(
                args.state_directory,
                deadline_monotonic=None if remaining is None else time.monotonic() + remaining,
                cancellation_check=None if budget is None else budget.cancellation_check,
                _writer_lease=owner,
            ))
            if any(
                observed_generations.get(model) != generation
                for model, generation in generations.items()
            ):
                raise StatePublicationRecoveryRequired(
                    "Semantic results do not match all published model heads"
                )
            return _observe_integrated_heads(
                args.state_directory,
                work_budget=budget,
                writer_connection=owner,
            )
    except PublicationHeadsError as exc:
        raise StatePublicationRecoveryRequired(str(exc)) from exc


def _semantic_results_ready(
    captured_results: list[tuple[str, object]],
    *,
    selected_sources: tuple[str, ...],
    image_available: bool,
) -> bool:
    required_scopes = set()
    if selected_sources:
        required_scopes.add("text")
    if image_available:
        required_scopes.add("image")
    by_scope = dict(captured_results)
    if not required_scopes.issubset(by_scope):
        return False
    for scope in required_scopes:
        value = by_scope[scope]
        if getattr(value, "complete", False) is not True:
            return False
        for generation in getattr(value, "generations", ()):
            summary = getattr(generation, "summary", None)
            if (
                getattr(summary, "status", None) != "ready"
                or getattr(summary, "unfinished", 0) != 0
                or getattr(summary, "errors", 0) != 0
                or getattr(summary, "stale", 0) != 0
            ):
                return False
    return True


def _record_integrated_semantic_work(
    args: argparse.Namespace,
    run_id: int | None,
    captured_results: list[tuple[str, object]],
) -> None:
    """Account observed Semantic work in the Framework run ledger."""

    if run_id is None:
        return
    from neocortex.persistence.framework_state_writer import FrameworkState

    items = 0
    for _scope, result in captured_results:
        value = getattr(result, "items_staged", 0)
        if type(value) is int and value > 0:
            items += value
    with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
        reader = getattr(state, "read_run_budget", None)
        if not callable(reader) or reader(run_id) is None:
            return
        run_row = state._connection.execute(
            "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        # Direct callers may attach Semantic metadata to a historical source
        # run (the CLI resume path uses a new running continuation).  A
        # terminal source cannot accept a new Framework-budget reservation;
        # keep that compatibility path state-only.
        if run_row is None or str(run_row[0]) != "running":
            return
        reservation = f"semantic:work:{items}:{len(captured_results)}"
        reserve = getattr(state, "reserve_run_stage", None)
        if callable(reserve):
            reserve(
                run_id,
                "semantic",
                reservation,
                items=items,
                bytes=0,
                worker="semantic",
            )
        else:
            state.reserve_run_budget(
                run_id,
                reservation,
                items=items,
                bytes=0,
                worker="semantic",
                stage="semantic",
            )


def _resolve_integrated_publication_after_nonterminal(
    state_directory: Path,
    publication: object | None,
) -> bool:
    """Abort a prepared publication only after the owner-head CAS is proven."""

    if publication is None:
        return True
    from neocortex.persistence.state_publication import (
        canonical_owner_heads,
        read_state_publication_state,
    )

    view = read_state_publication_state(state_directory)
    if view.status != "blocked":
        return view.status in {"absent", "complete"}
    prepared = getattr(publication, "prepared", None)
    event_id = getattr(prepared, "event_id", None)
    baseline = getattr(prepared, "owner_heads", ())
    pending = tuple(item for item in view.pending if item.event_id == event_id)
    if len(pending) != 1:
        return False
    observed = _observe_integrated_heads(state_directory)
    if baseline:
        if canonical_owner_heads(observed) != canonical_owner_heads(baseline):
            return False
        abort = getattr(publication, "abort", None)
        if not callable(abort):
            return False
        abort(observed, detail="Semantic stage ended without a complete generation")
        return True
    if view.epoch.epoch != 0 or view.publication is not None:
        return False
    _recover_pending_integrated_publication(state_directory)
    return True


def _select_integrated_sources(args: argparse.Namespace, run_id: int | None):
    """Keep independent readable sources usable and distinguish empty inputs."""

    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.platform.content_capability_manifest import content_capability_for_source
    from neocortex.semantic.semantic_sources import (
        TEXT_SOURCE_KINDS,
        semantic_source_database,
        semantic_source_heads,
    )

    route_states: dict[str, tuple[str, int]] = {}
    if run_id is not None:
        with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
            rows = state._connection.execute(
                "SELECT route_name,status,summary_json FROM route_runs WHERE run_id=? LIMIT 32", (run_id,)
            ).fetchall()
            for route_name, status, summary_json in rows:
                summary = {} if summary_json is None else json.loads(summary_json)
                count = summary.get("candidates", 0) if isinstance(summary, dict) else 0
                route_states[str(route_name)] = (str(status), count if type(count) is int else 0)
    explicit = args.semantic_source is not None
    requested = tuple(args.semantic_source) if explicit else tuple(TEXT_SOURCE_KINDS)
    selected: list[str] = []
    empty: list[str] = []
    blocked: dict[str, str] = {}

    def available(source: str, *, inspect_content: bool) -> bool:
        route = content_capability_for_source(source).route_name
        status, candidates = route_states.get(route, ("unobserved", 0))
        if status in {"failed", "cancelled", "interrupted"}:
            blocked[source] = "route_unavailable"
            return False
        if not semantic_source_database(args.state_directory, source).is_file():
            if (explicit and source != "image") or candidates > 0:
                blocked[source] = "source_missing"
            else:
                empty.append(source)
            return False
        if inspect_content:
            head = semantic_source_heads(args.state_directory, (source,))[0]
            if not head.complete:
                blocked[source] = head.reason or f"source_{head.coverage}"
                return False
            if head.row_count == 0:
                empty.append(source)
                return False
        return True

    for source in requested:
        if available(source, inspect_content=True):
            selected.append(source)
    images = available("image", inspect_content=True)
    args._semantic_source_unavailable = blocked
    args._semantic_source_empty = tuple(empty)
    return tuple(selected), images


def run_integrated_all_semantic_index(
    args: argparse.Namespace,
    *,
    progress: ProgressCallback | None = None,
    result_sink: Callable[[str, object], None] | None = None,
    print_output: bool = True,
    run_id: int | None = None,
    resume_source_run_id: int | None = None,
    framework_lock_held: bool = False,
) -> int:
    """Complete all applicable local sources, using one shared work budget.

    Explicit limits remain effective across text, image and OCR. Empty sources
    do not load models, and a partial generation is retained but never reported
    as complete.
    """

    if not args.all and resume_source_run_id is None:
        raise ValueError("integrated Semantic indexing requires --all or a resumable --resume-run")
    if not framework_lock_held:
        from neocortex.runtime.control.locking import FrameworkRunLock

        _validate_semantic_state_write(args.state_directory, database=True)
        args.state_directory.mkdir(parents=True, exist_ok=True)
        with FrameworkRunLock(args.state_directory / "framework.lock"):
            return run_integrated_all_semantic_index(
                args, progress=progress, result_sink=result_sink, print_output=print_output,
                run_id=run_id, resume_source_run_id=resume_source_run_id, framework_lock_held=True,
            )
    from neocortex.semantic.semantic_sources import semantic_source_database
    from neocortex.semantic.semantic_source_budget import semantic_source_read_budget
    from neocortex.semantic.semantic_work_budget import SemanticIndexDeadlineExceeded

    try:
        integrated_args = (
            _semantic_resume_args(args, resume_source_run_id)
            if resume_source_run_id is not None
            else argparse.Namespace(**vars(args))
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if print_output:
            _print_console_line(f"ERROR semantic-resume {type(exc).__name__}: {exc}")
        return 2
    if integrated_args is None:
        return 0
    integrated_args._semantic_work_budget = _integrated_semantic_budget(integrated_args, run_id)
    resume_source = resume_source_run_id
    image_cache_exists = semantic_source_database(
        integrated_args.state_directory,
        "image",
    ).is_file()
    image_available = (
        bool(integrated_args._semantic_resume_image_available)
        if resume_source is not None
        else image_cache_exists
    )
    if image_available and not image_cache_exists:
        raise StatePublicationRecoveryRequired("original image source is unavailable")
    if resume_source is None or getattr(integrated_args, "_semantic_selection_pending", False):
        try:
            with semantic_source_read_budget(integrated_args._semantic_work_budget):
                integrated_args.semantic_source, image_available = _select_integrated_sources(integrated_args, run_id)
        except SemanticIndexDeadlineExceeded as exc:
            _record_integrated_semantic_stage(
                integrated_args, run_id, "partial",
                details=_integrated_stage_details(
                    integrated_args, selected_sources=(), image_available=False,
                    error=exc, semantic_exit_code=2,
                ),
                idempotency_key="semantic:source-budget-exhausted",
            )
            return 2
    selected_sources = tuple(integrated_args.semantic_source or ())
    integrated_args.semantic_index = (
        "all" if selected_sources and image_available else "text" if selected_sources else "image"
    )
    if not selected_sources and not image_available:
        unavailable = bool(getattr(integrated_args, "_semantic_source_unavailable", {}))
        _record_integrated_semantic_stage(
            integrated_args,
            run_id,
            "partial" if unavailable else "skipped",
            details=_integrated_stage_details(
                integrated_args,
                selected_sources=selected_sources,
                image_available=image_available,
                resume_source_run_id=resume_source,
            ),
            idempotency_key="semantic:unavailable" if unavailable else "semantic:skipped",
        )
        if print_output:
            print(
                "SEMANTIC_ALL status=partial reason=source_unavailable"
                if unavailable else "SEMANTIC_ALL status=skipped reason=no_durable_text_or_image_cache"
            )
        emit_progress(
            progress,
            ProgressEvent(
                "semantic",
                "integrated",
                "Semantic incompleto: fuentes no disponibles" if unavailable else "Semantic sin contenido aplicable",
                1,
                1,
                "fase",
                True,
            ),
        )
        return 2 if unavailable else 0
    _record_integrated_semantic_stage(
        integrated_args,
        run_id,
        "running",
        details=_integrated_stage_details(
            integrated_args,
            selected_sources=selected_sources,
            image_available=image_available,
            resume_source_run_id=resume_source,
        ),
        idempotency_key="semantic:started",
    )
    try:
        publication = _begin_integrated_publication(
            integrated_args,
            run_id,
            selected_sources=selected_sources,
            image_available=image_available,
        )
        if publication is not None:
            integrated_args._semantic_publication_event_id = publication.prepared.event_id
    except BaseException as exc:
        _record_integrated_semantic_stage(
            integrated_args,
            run_id,
            "failed",
            details=_integrated_stage_details(
                integrated_args,
                selected_sources=selected_sources,
                image_available=image_available,
                error=exc,
                recovery_required=True,
                resume_source_run_id=resume_source,
            ),
            idempotency_key="semantic:publication-failed",
        )
        raise
    if print_output:
        print(
            "SEMANTIC_ALL status=starting "
            f"sources={','.join(selected_sources)} "
            f"max_items={integrated_args.semantic_max_items} "
            f"max_new_jobs={integrated_args.semantic_max_new_jobs} "
            f"time_budget_seconds={integrated_args.semantic_time_budget_seconds if integrated_args.semantic_time_budget_seconds is not None else 'unlimited'} "
            f"images={int(image_available)} "
        )
    emit_progress(
        progress,
        ProgressEvent(
            "semantic",
            "integrated",
            "Inicializando Semantic",
            0,
            1,
            "fase",
            metrics=(ProgressMetric("sources", len(selected_sources)),),
        ),
    )
    semantic_exit_code: int | None = None
    captured_results: list[tuple[str, object]] = []
    stage_complete = False
    recovery_required = False

    def capture_result(scope: str, value: object) -> None:
        captured_results.append((scope, value))
        if result_sink is not None:
            result_sink(scope, value)

    try:
        semantic_exit_code = run_semantic_index(
            integrated_args,
            incomplete_is_error=True,
            progress=progress,
            result_sink=capture_result,
            print_output=print_output,
            framework_lock_held=framework_lock_held,
        )
        semantic_failure = getattr(integrated_args, "_semantic_failure", None)
        semantic_failure = (
            semantic_failure if isinstance(semantic_failure, BaseException) else None
        )
        if semantic_failure is not None:
            args._semantic_failure = semantic_failure
        args._semantic_scope_unavailable = dict(getattr(integrated_args, "_semantic_scope_unavailable", {}))
        _record_integrated_semantic_work(
            integrated_args,
            run_id,
            captured_results,
        )
        stage_complete = semantic_exit_code == 0 and _semantic_results_ready(
            captured_results,
            selected_sources=selected_sources,
            image_available=image_available,
        )
        if stage_complete and publication is not None:
            final_heads = _final_publication_owner_heads(
                integrated_args,
                captured_results,
                selected_sources=selected_sources,
            )
            integrated_args._semantic_work_budget.checkpoint()
            publication.commit(
                final_heads,
                verify_owner_heads=lambda: _observe_integrated_heads(
                    integrated_args.state_directory,
                    work_budget=integrated_args._semantic_work_budget,
                ),
            )
        elif publication is not None:
            try:
                recovery_required = not _resolve_integrated_publication_after_nonterminal(
                    integrated_args.state_directory,
                    publication,
                )
            except BaseException:
                recovery_required = True
            if recovery_required:
                semantic_exit_code = 2
        if not stage_complete and semantic_exit_code == 0:
            semantic_exit_code = 2
        if getattr(integrated_args, "_semantic_source_unavailable", {}):
            semantic_exit_code = 2
        terminal_status = "completed" if stage_complete and semantic_exit_code == 0 else "partial"
        _record_integrated_semantic_stage(
            integrated_args,
            run_id,
            terminal_status,
            details=_integrated_stage_details(
                integrated_args,
                selected_sources=selected_sources,
                image_available=image_available,
                semantic_exit_code=semantic_exit_code,
                error=semantic_failure,
                recovery_required=recovery_required,
                resume_source_run_id=resume_source,
            ),
            idempotency_key=f"semantic:terminal:{semantic_exit_code}",
        )
        return semantic_exit_code
    except KeyboardInterrupt as exc:
        try:
            recovery_required = not _resolve_integrated_publication_after_nonterminal(
                integrated_args.state_directory,
                publication,
            )
        except BaseException:
            recovery_required = True
        _record_integrated_semantic_stage(
            integrated_args,
            run_id,
            "interrupted",
            details=_integrated_stage_details(
                integrated_args,
                selected_sources=selected_sources,
                image_available=image_available,
                error=exc,
                recovery_required=recovery_required,
                resume_source_run_id=resume_source,
            ),
            idempotency_key="semantic:terminal:interrupted",
        )
        raise
    except BaseException as exc:
        try:
            recovery_required = not _resolve_integrated_publication_after_nonterminal(
                integrated_args.state_directory,
                publication,
            )
        except BaseException:
            recovery_required = True
        _record_integrated_semantic_stage(
            integrated_args,
            run_id,
            "failed",
            details=_integrated_stage_details(
                integrated_args,
                selected_sources=selected_sources,
                image_available=image_available,
                error=exc,
                recovery_required=recovery_required,
                resume_source_run_id=resume_source,
            ),
            idempotency_key="semantic:terminal:failed",
        )
        raise
    finally:
        emit_progress(
            progress,
            ProgressEvent(
                "semantic",
                "integrated",
                (
                    "Semantic completado"
                    if stage_complete and semantic_exit_code == 0
                    else "Semantic interrumpido"
                    if semantic_exit_code is None
                    else "Semantic pausado con progreso"
                ),
                1 if semantic_exit_code is not None else 0,
                1,
                "fase",
                True,
                (
                    ProgressMetric("sources", len(selected_sources)),
                    ProgressMetric(
                        "status",
                        "ok"
                        if stage_complete and semantic_exit_code == 0
                        else "interrumpido"
                        if semantic_exit_code is None
                        else "partial"
                        if semantic_exit_code == 0
                        else "error",
                    ),
                ),
            ),
        )


def run_semantic_search(args: argparse.Namespace) -> int:
    """Search requested rankings independently and print rank-only fusion."""

    from neocortex.semantic.semantic_service import SEMANTIC_DATABASE_NAME

    database = args.state_directory / SEMANTIC_DATABASE_NAME
    cancellation_check = _semantic_cancellation_checkpoint(args)
    try:
        exact_index = _open_exact_index_for_search(
            args,
            database,
            cancellation_check=cancellation_check,
        )
    except Exception as exc:
        return _semantic_failure("semantic-exact-index-open", exc, offline=False)
    if exact_index is None:
        return _run_semantic_search_with_handle(
            args,
            None,
            cancellation_check=cancellation_check,
        )
    try:
        return _run_semantic_search_with_handle(
            args,
            exact_index,
            cancellation_check=cancellation_check,
        )
    finally:
        _close_exact_index_handle(args.semantic_exact_index, exact_index)


def _run_semantic_search_with_handle(
    args: argparse.Namespace,
    exact_index: ExactIndexHandle | None,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> int:
    """Run the search while the optional verified handle remains alive."""

    from neocortex.semantic.semantic_service import search_semantic_index

    mode = args.semantic_search_mode
    diagnostic_item_ids: tuple[str, ...] = tuple(
        getattr(args, "semantic_diagnostic_item", ()) or ()
    )
    search_kwargs: _SemanticSearchKeywordArgs = {
        "limit": args.semantic_search_limit,
        "max_vectors": args.semantic_max_vectors,
        "include_text": mode in {"all", "text"},
        "include_images": mode in {"all", "image"},
        "include_lexical": mode in {"all", "lexical"},
        "text_model": _semantic_text_model(args.semantic_text_profile),
        "model_cache": args.semantic_model_cache,
        "local_files_only": True,
        "threads": args.semantic_threads,
        "diagnostic_item_ids": diagnostic_item_ids,
    }
    if cancellation_check is not None:
        search_kwargs["cancellation_check"] = cancellation_check
    if exact_index is not None:
        search_kwargs["exact_index"] = exact_index
    try:
        result = search_semantic_index(
            args.state_directory,
            args.semantic_search,
            **search_kwargs,
        )
    except Exception as exc:  # FTS/ONNX backends expose distinct exceptions
        if exact_index is not None:
            _print_exact_index_usage(args.semantic_exact_index, exact_index)
        return _semantic_failure("semantic-search", exc, offline=mode != "lexical")

    available_rankings = 0
    calibrated_abstentions = 0
    for semantic_ranking in result.rankings:
        available_rankings += int(semantic_ranking.available)
        calibration = semantic_ranking.provenance.get("retrieval_abstention")
        calibrated_abstained = bool(
            isinstance(calibration, dict) and calibration.get("query_abstained") is True
        )
        calibrated_abstentions += int(calibrated_abstained)
        abstention_reason = (
            calibration.get("abstention_reason")
            if isinstance(calibration, dict) and calibrated_abstained
            else None
        )
        reason = semantic_ranking.unavailable_reason or semantic_ranking.cutoff_reason or "-"
        cutoff_score = (
            "-" if semantic_ranking.cutoff_score is None else f"{semantic_ranking.cutoff_score:.6f}"
        )
        next_cursor = semantic_ranking.next_cursor or "-"
        _print_console_line(
            f"SEMANTIC_RANKING name={semantic_ranking.name} "
            f"available={int(semantic_ranking.available)} "
            f"complete={int(semantic_ranking.complete)} "
            f"scanned={semantic_ranking.scanned} "
            f"hits={len(semantic_ranking.hits)} "
            f"abstained={int(calibrated_abstained)} "
            f"abstention_reason={abstention_reason or '-'} "
            f"weight={semantic_ranking.fusion_weight:.6f} "
            f"reason={reason} cutoff_score={cutoff_score} "
            f"next_cursor={next_cursor} "
            "provenance="
            f"{json.dumps(semantic_ranking.provenance, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
        )
    for lexical_ranking in result.lexical_rankings:
        availability = lexical_ranking.availability.value
        available_rankings += int(availability == "available")
        _print_console_line(
            f"LEXICAL_RANKING name={lexical_ranking.ranking_name} "
            f"availability={availability} hits={len(lexical_ranking.hits)} "
            f"reason={lexical_ranking.unavailable_reason or '-'}"
        )
    _print_console_line(
        f"SEMANTIC_SEARCH query={json.dumps(result.query, ensure_ascii=False)} "
        f"complete={int(result.complete)} available_rankings={available_rankings} "
        f"calibrated_abstentions={calibrated_abstentions} "
        f"fused_hits={len(result.fused)}"
    )
    if diagnostic_item_ids:
        # The search service already collected these bounded target
        # diagnostics while traversing the funnel.  Project them into one
        # ordered trace without rerunning retrieval or opening another owner.
        from neocortex.knowledge.knowledge_operational_query import semantic_item_diagnostic

        for item_id in diagnostic_item_ids:
            trace = semantic_item_diagnostic(result, item_id)
            _print_console_line(
                "SEMANTIC_ITEM_DIAGNOSTIC "
                f"item={json.dumps(item_id, ensure_ascii=False)} "
                "trace="
                f"{json.dumps(trace, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
            )
    for rank, hit in enumerate(result.fused, start=1):
        evidence = ",".join(
            f"{value.ranking}:{value.rank}:{value.raw_score:.6f}:{value.contribution:.6f}"
            for value in hit.fused.evidence
        )
        _print_console_line(
            f"SEMANTIC_HIT rank={rank} score={hit.fused.score:.6f} "
            f"item={hit.fused.item_id} source={hit.source_kind} "
            f"identity={json.dumps(hit.source_identity, ensure_ascii=False)} "
            f"path={json.dumps(hit.path, ensure_ascii=False)} "
            f"snippet={json.dumps(hit.snippet, ensure_ascii=False)} "
            f"evidence={evidence or '-'}"
        )
    if exact_index is not None:
        _print_exact_index_usage(args.semantic_exact_index, exact_index)
    return 0 if result.complete and available_rankings else 2


def run_semantic_image_calibrate(args: argparse.Namespace) -> int:
    """Measure and persist a bounded local image-retrieval calibration."""

    from neocortex.runtime.control.locking import FrameworkRunLock
    from neocortex.semantic.semantic_service import (
        calibrate_image_retrieval,
        persist_image_retrieval_calibration,
    )

    try:
        _validate_semantic_state_write(
            args.state_directory,
            database=True,
        )
        with FrameworkRunLock(args.state_directory / "framework.lock"):
            calibration, evidence = calibrate_image_retrieval(
                args.state_directory,
                args.semantic_image_calibrate,
                model_cache=args.semantic_model_cache,
                local_files_only=True,
                threads=args.semantic_threads,
                max_vectors=args.semantic_max_vectors,
            )
            persist_image_retrieval_calibration(
                args.state_directory / "semantic.sqlite3",
                calibration,
                evidence,
            )
    except Exception as exc:
        return _semantic_failure("semantic-image-calibrate", exc, offline=True)
    print(
        f"SEMANTIC_IMAGE_CALIBRATION signature={calibration.calibration_signature} "
        f"generation={evidence.generation_id} sample_items={calibration.sample_items} "
        f"positive_queries={calibration.positive_queries} "
        f"negative_queries={calibration.negative_queries} "
        f"positive_floor={evidence.positive_floor:.6f} "
        f"negative_ceiling={evidence.negative_ceiling:.6f} "
        f"minimum_score={calibration.minimum_score:.6f} "
        f"processing_signature={calibration.indexed_processing_signature}"
    )
    return 0


def run_semantic_classify(args: argparse.Namespace) -> int:
    """Materialize ontology scores as advisory evidence only."""

    from neocortex.runtime.control.locking import FrameworkRunLock
    from neocortex.semantic.semantic_service import classify_semantic_index

    target = args.semantic_classify
    try:
        _validate_semantic_state_write(
            args.state_directory,
            database=True,
        )
        with FrameworkRunLock(args.state_directory / "framework.lock"):
            result = classify_semantic_index(
                args.state_directory,
                include_text=target in {"text", "all"},
                include_images=target in {"image", "all"},
                text_model=_semantic_text_model(args.semantic_text_profile),
                model_cache=args.semantic_model_cache,
                local_files_only=True,
                threads=args.semantic_threads,
            )
    except Exception as exc:  # model runtimes expose backend-specific exceptions
        return _semantic_failure("semantic-classify", exc, offline=True)
    skipped = json.dumps(
        result.skipped,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    print(
        f"SEMANTIC_CLASSIFICATION ontology={result.ontology_id} "
        f"ontology_version={result.ontology_version} advisory=1 "
        f"passes={len(result.passes)} skipped={skipped} "
        f"database={result.semantic_database}"
    )
    for evidence_pass in result.passes:
        print(
            f"SEMANTIC_EVIDENCE_PASS space={evidence_pass.vector_space} "
            f"indexed_model={evidence_pass.indexed_model_signature} "
            f"query_model={evidence_pass.query_model_signature} "
            f"prototypes={evidence_pass.prototypes} "
            f"entities={evidence_pass.entities_scored} "
            f"abstained={evidence_pass.entities_abstained} "
            f"evidence={evidence_pass.evidence_staged} "
            f"stale_deactivated={evidence_pass.stale_evidence_deactivated} "
            "authority=advisory"
        )
    return 2 if result.skipped else 0


def run_semantic_evidence(args: argparse.Namespace) -> int:
    """List current advisory ontology evidence without opening writable state."""

    from neocortex.semantic.semantic_ontology import ONTOLOGY_VERSION
    from neocortex.semantic.semantic_service import SEMANTIC_DATABASE_NAME, SEMANTIC_ONTOLOGY_ID
    from neocortex.semantic.semantic_state import list_semantic_evidence

    database = args.state_directory / SEMANTIC_DATABASE_NAME
    try:
        queried = list_semantic_evidence(
            database,
            item_id=args.semantic_evidence,
            ontology_id=SEMANTIC_ONTOLOGY_ID,
            ontology_version=ONTOLOGY_VERSION,
            limit=args.semantic_evidence_limit + 1,
        )
    except Exception as exc:  # read boundary converts SQLite errors to CLI status
        return _semantic_failure("semantic-evidence", exc, offline=False)
    truncated = len(queried) > args.semantic_evidence_limit
    evidence = queried[: args.semantic_evidence_limit]
    print(
        f"SEMANTIC_EVIDENCE item={args.semantic_evidence} count={len(evidence)} "
        f"limit={args.semantic_evidence_limit} truncated={int(truncated)} "
        f"ontology={SEMANTIC_ONTOLOGY_ID} ontology_version={ONTOLOGY_VERSION} "
        "authority=advisory"
    )
    for value in evidence:
        provenance = json.dumps(
            value.provenance,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        print(
            f"SEMANTIC_CONCEPT rank={value.rank} concept={value.concept_id} "
            f"score={value.score:.6f} entity={value.source_entity_id} "
            f"space={value.vector_space} generation={value.generation_id or '-'} "
            f"calibration={value.calibration_status.value} "
            f"disposition={value.disposition.value} authority=advisory "
            f"provenance={provenance}"
        )
    return 0


# endregion [01]
