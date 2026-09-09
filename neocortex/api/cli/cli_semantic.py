"""Direct multimodal Semantic CLI operations."""

from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

if TYPE_CHECKING:
    from neocortex.semantic.semantic_models import EmbeddingModelSpec
    from neocortex.semantic.semantic_service_contracts import SemanticIndexResult
    from neocortex.semantic.semantic_work_budget import SemanticWorkBudget

__all__ = [
    "run_integrated_all_semantic_index",
    "run_semantic_classify",
    "run_semantic_evidence",
    "run_semantic_image_calibrate",
    "run_semantic_index",
    "run_semantic_plan",
    "run_semantic_prepare_models",
    "run_semantic_search",
    "run_semantic_status",
    "semantic_resume_available",
]

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
    return tuple(
        source_kind
        for source_kind in TEXT_SOURCE_KINDS
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
            f"errors={summary.errors} stale={summary.stale}"
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
    code_link_statuses: list[tuple[int, str, int, int]] = field(default_factory=list)
    scope_timings: list[tuple[str, int]] = field(default_factory=list)


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
                "no durable PDF, DOCX, Office, audio or code text cache is available"
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

    text_model = _semantic_text_model(args.semantic_text_profile)
    selected_sources = _selected_semantic_text_sources(args)
    work_budget = SemanticWorkBudget.from_time_budget(
        max_items=args.semantic_max_items,
        max_new_jobs=args.semantic_max_new_jobs,
        time_budget_seconds=args.semantic_time_budget_seconds,
    )
    execution = _SemanticIndexExecution(
        args=args,
        text_model=text_model,
        selected_sources=selected_sources,
        work_budget=work_budget,
        progress=progress,
        result_sink=result_sink,
    )
    try:
        _validate_semantic_state_write(
            args.state_directory,
            database=True,
        )
        def execute_scopes() -> None:
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
    except Exception as exc:  # model runtimes expose backend-specific exceptions
        return _semantic_index_failure(execution, exc, print_output=print_output)
    return _complete_semantic_index_execution(
        execution,
        incomplete_is_error=incomplete_is_error,
        print_output=print_output,
    )


def _execute_semantic_index_scopes(
    execution: _SemanticIndexExecution,
    *,
    text_operation: Callable[..., SemanticIndexResult],
    image_operation: Callable[..., SemanticIndexResult],
) -> None:
    _execute_semantic_text_index(execution, text_operation)
    _execute_semantic_image_index(execution, image_operation)


def _execute_semantic_text_index(
    execution: _SemanticIndexExecution,
    operation: Callable[..., SemanticIndexResult],
) -> None:
    args = execution.args
    if args.semantic_index not in {"text", "all"}:
        return
    if not execution.selected_sources:
        raise FileNotFoundError(
            "no durable PDF, DOCX, Office, audio or code text cache is available"
        )
    started = time.perf_counter_ns()
    try:
        result = operation(
            args.state_directory,
            source_kinds=execution.selected_sources,
            model=execution.text_model,
            model_cache=args.semantic_model_cache,
            local_files_only=True,
            threads=args.semantic_threads,
            work_budget=execution.work_budget,
            progress=execution.progress,
        )
    finally:
        execution.scope_timings.append(("text", time.perf_counter_ns() - started))
    _record_semantic_index_result(execution, "text", result)
    code_link_status = _current_semantic_code_link_status(args.state_directory, result)
    if code_link_status is not None:
        execution.code_link_statuses.append(code_link_status)


def _execute_semantic_image_index(
    execution: _SemanticIndexExecution,
    operation: Callable[..., SemanticIndexResult],
) -> None:
    args = execution.args
    if args.semantic_index not in {"image", "all"} or execution.work_budget.truncated:
        return
    started = time.perf_counter_ns()
    try:
        result = operation(
            args.state_directory,
            model_cache=args.semantic_model_cache,
            local_files_only=True,
            threads=args.semantic_threads,
            embed_ocr_text=not args.semantic_no_ocr,
            ocr_model=execution.text_model,
            work_budget=execution.work_budget,
            progress=execution.progress,
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


def _current_semantic_code_link_status(
    state_directory: Path,
    result: SemanticIndexResult,
) -> tuple[int, str, int, int] | None:
    if "code" not in result.sources or not result.complete:
        return None
    from neocortex.code.search.code_semantic_links import current_code_embedding_link_counts

    summary = result.generations[0].summary
    active_links, current_links = current_code_embedding_link_counts(
        state_directory,
        generation_id=summary.generation_id,
        model_signature=summary.model_signature,
    )
    return (
        summary.generation_id,
        summary.model_signature,
        active_links,
        current_links,
    )


def _semantic_index_failure(
    execution: _SemanticIndexExecution,
    exc: Exception,
    *,
    print_output: bool,
) -> int:
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
        offline=True,
        print_output=print_output,
    )


def _complete_semantic_index_execution(
    execution: _SemanticIndexExecution,
    *,
    incomplete_is_error: bool,
    print_output: bool,
) -> int:
    failed = False
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
    _print_semantic_code_link_statuses(
        execution.code_link_statuses,
        print_output=print_output,
    )
    return 2 if failed else 0


def _semantic_index_result_failed(
    result: SemanticIndexResult,
    *,
    incomplete_is_error: bool,
) -> bool:
    if not incomplete_is_error and result.truncated and result.errors == 0 and result.stale == 0:
        return False
    return not result.complete


def _print_semantic_code_link_statuses(
    statuses: list[tuple[int, str, int, int]],
    *,
    print_output: bool,
) -> None:
    if not print_output:
        return
    for (
        generation_id,
        model_signature,
        active_links,
        current_links,
    ) in statuses:
        print(
            f"SEMANTIC_CODE_LINKS generation={generation_id} "
            f"model={model_signature} active={active_links} "
            f"current={current_links} stale={active_links - current_links} "
            "authority=retrieval_evidence_only "
            "calibration=uncalibrated_similarity"
        )


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
    if (
        type(max_items) is not int
        or max_items < 1
        or type(max_new_jobs) is not int
        or max_new_jobs < 1
    ):
        raise RuntimeError(f"run {source_run_id} Semantic budget is invalid")
    if isinstance(time_budget, bool) or not isinstance(time_budget, (int, float)):
        raise RuntimeError(f"run {source_run_id} Semantic budget is invalid")
    time_budget_value = time_budget
    if not 0.001 <= float(time_budget_value) <= 172_800.0:
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
        "time_budget_seconds": float(time_budget_value),
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
    details = spec.get("details")
    if (
        not isinstance(selected_sources, tuple)
        or any(not isinstance(value, str) for value in selected_sources)
        or not isinstance(selection_pending, bool)
        or not isinstance(image_available, bool)
        or type(max_items) is not int
        or type(max_new_jobs) is not int
        or isinstance(time_budget_seconds, bool)
        or not isinstance(time_budget_seconds, (int, float))
        or not isinstance(details, Mapping)
    ):
        raise RuntimeError(f"run {source_run_id} Semantic resume specification is invalid")
    effective.semantic_source = None if selection_pending else list(selected_sources)
    effective.semantic_max_items = max_items
    effective.semantic_max_new_jobs = max_new_jobs
    effective.semantic_time_budget_seconds = float(time_budget_seconds)
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
    return effective


def _recover_pending_integrated_publication(state_directory: Path) -> bool:
    """Resolve an old prepare only when the publication API proves its scope."""

    from neocortex.persistence.state_publication import (
        abort_state_publication,
        abort_unbound_state_publication,
        read_state_publication_state,
    )

    view = read_state_publication_state(state_directory)
    if view.status != "blocked":
        return False
    pending = tuple(item for item in view.pending if item.operation == "framework-all-semantic")
    if len(pending) != 1 or len(pending) != len(view.pending):
        raise RuntimeError("Semantic publication recovery_required: another publication is pending")
    prepared = pending[0]
    observed = view.epoch.owner_heads
    if prepared.owner_heads:
        if observed != prepared.owner_heads:
            raise RuntimeError("Semantic publication recovery_required: owner-head drift")
        abort_state_publication(
            state_directory,
            event_id=prepared.event_id,
            observed_owner_heads=observed,
            expected_epoch=view.epoch.epoch,
            detail="Semantic resume verified unchanged owner heads before abort",
        )
        return True
    if view.epoch.epoch != 0 or view.publication is not None:
        raise RuntimeError(
            "Semantic publication recovery_required: unbound prepare is not at epoch zero"
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
    """Prepare the logical Semantic/Code publication gate for an ``--all`` run."""

    if run_id is None:
        return None
    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.persistence.state_publication import (
        begin_state_publication,
        publication_idempotency_key,
        read_state_publication_state,
    )

    _recover_pending_integrated_publication(args.state_directory)
    with FrameworkState(args.state_directory / "framework.sqlite3", existing_only=True) as state:
        manifest = state.read_run_manifest(run_id)
    if manifest is None:
        raise RuntimeError(f"run {run_id} has no manifest for Semantic publication")
    owners = ("semantic", "code") if "code" in selected_sources else ("semantic",)
    key = publication_idempotency_key(
        "framework-all-semantic",
        run_id,
        selected_sources,
        image_available,
    )
    view = read_state_publication_state(args.state_directory)
    if view.status not in {"absent", "complete"}:
        raise RuntimeError("Semantic publication recovery_required: state publication is not ready")
    baseline_heads = tuple(head for head in view.epoch.owner_heads if head.owner in owners)
    if {head.owner for head in baseline_heads} != set(owners):
        baseline_heads = ()
    return begin_state_publication(
        args.state_directory,
        operation="framework-all-semantic",
        owners=owners,
        idempotency_key=key,
        manifest_sha256=str(manifest["digest"])[len("sha256:") :],
        detail="Semantic owner work is pending its terminal lifecycle publication",
        owner_heads=baseline_heads or None,
    )


def _final_publication_owner_heads(
    args: argparse.Namespace,
    captured_results: list[tuple[str, object]],
    *,
    selected_sources: tuple[str, ...],
):
    """Derive bounded owner-head identities from published Semantic results."""

    from neocortex.persistence.state_publication import StateOwnerHead

    generations: list[tuple[int, str]] = []
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
                generations.append((generation_id, model_signature))
    if not generations:
        raise RuntimeError("Semantic publication produced no owner generation")
    generation_id, model_signature = max(generations)
    semantic_payload = json.dumps(
        {
            "owner": "semantic",
            "generation_id": generation_id,
            "model_signature": model_signature,
            "sources": list(selected_sources),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    heads = [
        StateOwnerHead(
            owner="semantic",
            revision=generation_id,
            digest_sha256=hashlib.sha256(semantic_payload).hexdigest(),
        )
    ]
    if "code" in selected_sources:
        from neocortex.code.search.code_semantic_links import current_code_embedding_link_counts

        active, current = current_code_embedding_link_counts(
            args.state_directory,
            generation_id=generation_id,
            model_signature=model_signature,
        )
        code_payload = json.dumps(
            {
                "owner": "code",
                "generation_id": generation_id,
                "model_signature": model_signature,
                "active": active,
                "current": current,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        heads.append(
            StateOwnerHead(
                owner="code",
                revision=generation_id,
                digest_sha256=hashlib.sha256(code_payload).hexdigest(),
            )
        )
    return tuple(heads)


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
    from neocortex.persistence.state_publication import read_state_publication_state

    view = read_state_publication_state(state_directory)
    if view.status != "blocked":
        return view.status in {"absent", "complete"}
    prepared = getattr(publication, "prepared", None)
    event_id = getattr(prepared, "event_id", None)
    baseline = getattr(prepared, "owner_heads", ())
    pending = tuple(item for item in view.pending if item.event_id == event_id)
    if len(pending) != 1:
        return False
    observed = view.epoch.owner_heads
    if baseline:
        if observed != baseline:
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
    """Advance bounded document and image embeddings after ``--all`` routes.

    Broad Archive and Code inventories can contain thousands or millions of
    chunks, so both remain explicit ``--semantic-source`` choices.  The default
    integrated stage advances physical document/audio caches and treats a
    bounded truncation as resumable progress rather than as a failed framework
    run.
    """

    if not args.all and resume_source_run_id is None:
        raise ValueError("integrated Semantic indexing requires --all or a resumable --resume-run")
    from neocortex.semantic.semantic_sources import TEXT_SOURCE_KINDS, semantic_source_database

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
    resume_source = resume_source_run_id
    image_available = semantic_source_database(
        integrated_args.state_directory,
        "image",
    ).is_file()
    if args.semantic_source is None and (
        resume_source is None or getattr(integrated_args, "_semantic_selection_pending", False)
    ):
        integrated_args.semantic_source = tuple(
            source_kind
            for source_kind in TEXT_SOURCE_KINDS
            if source_kind not in {"archive", "code"}
            and semantic_source_database(integrated_args.state_directory, source_kind).is_file()
        )
    selected_sources = tuple(integrated_args.semantic_source or ())
    integrated_args.semantic_index = (
        "all" if selected_sources and image_available else "text" if selected_sources else "image"
    )
    if not selected_sources and not image_available:
        _record_integrated_semantic_stage(
            integrated_args,
            run_id,
            "skipped",
            details=_integrated_stage_details(
                integrated_args,
                selected_sources=selected_sources,
                image_available=image_available,
                resume_source_run_id=resume_source,
            ),
            idempotency_key="semantic:skipped",
        )
        if print_output:
            print("SEMANTIC_ALL status=skipped reason=no_durable_text_or_image_cache")
        emit_progress(
            progress,
            ProgressEvent(
                "semantic",
                "integrated",
                "Semantic omitido: no hay texto durable",
                1,
                1,
                "fase",
                True,
            ),
        )
        return 0
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
            f"time_budget_seconds={integrated_args.semantic_time_budget_seconds:g} "
            f"images={int(image_available)} "
            f"code_explicit={int('code' in selected_sources)}"
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
            incomplete_is_error=False,
            progress=progress,
            result_sink=capture_result,
            print_output=print_output,
            framework_lock_held=framework_lock_held,
        )
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
            publication.commit(
                _final_publication_owner_heads(
                    integrated_args,
                    captured_results,
                    selected_sources=selected_sources,
                )
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

    from neocortex.semantic.semantic_service import search_semantic_index

    mode = args.semantic_search_mode
    diagnostic_item_ids: tuple[str, ...] = tuple(
        getattr(args, "semantic_diagnostic_item", ()) or ()
    )
    try:
        result = search_semantic_index(
            args.state_directory,
            args.semantic_search,
            limit=args.semantic_search_limit,
            max_vectors=args.semantic_max_vectors,
            include_text=mode in {"all", "text"},
            include_images=mode in {"all", "image"},
            include_lexical=mode in {"all", "lexical"},
            text_model=_semantic_text_model(args.semantic_text_profile),
            model_cache=args.semantic_model_cache,
            local_files_only=True,
            threads=args.semantic_threads,
            diagnostic_item_ids=diagnostic_item_ids,
        )
    except Exception as exc:  # FTS/ONNX backends expose distinct exceptions
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
