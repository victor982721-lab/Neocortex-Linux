"""Direct multimodal Semantic CLI operations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

if TYPE_CHECKING:
    from .semantic_models import EmbeddingModelSpec
    from .semantic_service_contracts import SemanticIndexResult
    from .semantic_work_budget import SemanticWorkBudget

__all__ = [
    "run_integrated_all_semantic_index",
    "run_semantic_classify",
    "run_semantic_evidence",
    "run_semantic_index",
    "run_semantic_plan",
    "run_semantic_prepare_models",
    "run_semantic_search",
    "run_semantic_status",
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
    from .semantic_config import (
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

    from .internal_paths import canonical_internal_paths_policy
    from .inventory_boundary import (
        state_sqlite_mutation_paths,
        validate_authorized_state_path,
    )
    from .protected_content import canonical_protected_content_policy
    from .semantic_service import SEMANTIC_DATABASE_NAME

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
            "HINT semantic model use is offline-only for this action; if weights "
            "are missing run Neocortex --semantic-prepare-models first"
        )
    return 2


def _selected_semantic_text_sources(args: argparse.Namespace) -> tuple[str, ...]:
    from .semantic_sources import TEXT_SOURCE_KINDS, semantic_source_database

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


def run_semantic_status(args: argparse.Namespace) -> int:
    """Show bounded semantic state without creating or migrating it."""

    from .semantic_service import SEMANTIC_DATABASE_NAME, semantic_status

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
    return 0


def run_semantic_plan(args: argparse.Namespace) -> int:
    """Project Semantic work without creating locks, models, jobs or state."""

    from .semantic_service import plan_semantic_index, semantic_plan_payload

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

    from .locking import FrameworkRunLock
    from .semantic_config import default_semantic_model_cache
    from .semantic_service import prepare_semantic_models

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
) -> int:
    """Incrementally embed durable route state without authorizing downloads."""

    from .locking import FrameworkRunLock
    from .semantic_service import index_image_embeddings, index_text_embeddings
    from .semantic_work_budget import SemanticWorkBudget

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
        with FrameworkRunLock(args.state_directory / "framework.lock"):
            _execute_semantic_index_scopes(
                execution,
                text_operation=index_text_embeddings,
                image_operation=index_image_embeddings,
            )
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
    from .code_semantic_links import current_code_embedding_link_counts

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


def run_integrated_all_semantic_index(
    args: argparse.Namespace,
    *,
    progress: ProgressCallback | None = None,
    result_sink: Callable[[str, object], None] | None = None,
    print_output: bool = True,
) -> int:
    """Advance bounded document and image embeddings after ``--all`` routes.

    Broad Archive and Code inventories can contain thousands or millions of
    chunks, so both remain explicit ``--semantic-source`` choices.  The default
    integrated stage advances physical document/audio caches and treats a
    bounded truncation as resumable progress rather than as a failed framework
    run.
    """

    if not args.all:
        raise ValueError("integrated Semantic indexing requires --all")
    from .semantic_sources import TEXT_SOURCE_KINDS, semantic_source_database

    integrated_args = argparse.Namespace(**vars(args))
    image_available = semantic_source_database(
        args.state_directory,
        "image",
    ).is_file()
    if args.semantic_source is None:
        integrated_args.semantic_source = tuple(
            source_kind
            for source_kind in TEXT_SOURCE_KINDS
            if source_kind not in {"archive", "code"}
            and semantic_source_database(args.state_directory, source_kind).is_file()
        )
    selected_sources = tuple(integrated_args.semantic_source or ())
    integrated_args.semantic_index = (
        "all" if selected_sources and image_available else "text" if selected_sources else "image"
    )
    if not selected_sources and not image_available:
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
    if print_output:
        print(
            "SEMANTIC_ALL status=starting "
            f"sources={','.join(selected_sources)} "
            f"max_items={args.semantic_max_items} "
            f"max_new_jobs={args.semantic_max_new_jobs} "
            f"time_budget_seconds={args.semantic_time_budget_seconds:g} "
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
    try:
        semantic_exit_code = run_semantic_index(
            integrated_args,
            incomplete_is_error=False,
            progress=progress,
            result_sink=result_sink,
            print_output=print_output,
        )
        return semantic_exit_code
    finally:
        emit_progress(
            progress,
            ProgressEvent(
                "semantic",
                "integrated",
                (
                    "Semantic completado"
                    if semantic_exit_code == 0
                    else "Semantic interrumpido"
                    if semantic_exit_code is None
                    else "Semantic completado con incidencias"
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
                        if semantic_exit_code == 0
                        else "interrumpido"
                        if semantic_exit_code is None
                        else "error",
                    ),
                ),
            ),
        )


def run_semantic_search(args: argparse.Namespace) -> int:
    """Search requested rankings independently and print rank-only fusion."""

    from .semantic_service import search_semantic_index

    mode = args.semantic_search_mode
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


def run_semantic_classify(args: argparse.Namespace) -> int:
    """Materialize ontology scores as advisory evidence only."""

    from .locking import FrameworkRunLock
    from .semantic_service import classify_semantic_index

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

    from .semantic_ontology import ONTOLOGY_VERSION
    from .semantic_service import SEMANTIC_DATABASE_NAME, SEMANTIC_ONTOLOGY_ID
    from .semantic_state import list_semantic_evidence

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
