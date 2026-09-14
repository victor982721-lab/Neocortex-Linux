"""Preset expansion and bounded command-line validation."""

from __future__ import annotations
import argparse
import math

from neocortex.platform.policy import (
    LINUX_MUTATION_REASON,
    current_platform_policy,
    linux_mutation_requested,
)

from .cli_audio_surface import (
    validate_audio_arguments,
    validate_audio_direct_operation,
)
from .cli_archive_surface import (
    validate_archive_arguments,
    validate_archive_direct_operation,
)
from .cli_capabilities_surface import validate_capabilities_arguments
from .cli_config_doctor_surface import validate_config_doctor_arguments
from .cli_code_surface import validate_code_arguments
from .cli_content_diagnostics import validate_content_diagnostics_arguments
from .cli_docx_surface import validate_docx_arguments, validate_docx_direct_operation
from .cli_dedup_keeper import validate_dedup_keeper_arguments
from .cli_knowledge_surface import validate_knowledge_arguments
from .cli_models_surface import validate_models_arguments
from .cli_office_surface import (
    validate_office_arguments,
    validate_office_direct_operation,
)
from .cli_operations import DirectOperationFamily, selected_direct_operations
from .cli_platform_surface import validate_platform_arguments
from .cli_semantic_surface import validate_semantic_arguments
from .cli_text_surface import validate_text_arguments
from .cli_video_surface import (
    validate_video_arguments,
    validate_video_direct_operation,
)
from neocortex.safety.ocr_profiles import parse_language_spec
from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER,
    ORGANIZABLE_ROUTE_NAMES,
    normalize_route_selection,
)

# region [01] Stable presets

ALL_PRESET = {
    "route": "all",
    # Keep the integrated command conservative: Code only admits configured
    # project roots unless the caller explicitly opts into a broader scan.
    "code_candidate_scope": "projects",
    # The normal controlled-corpus workflow requests a third-party cleanup
    # plan.  ``--apply`` remains the separate physical-effect gate; without it
    # this is only a preview and does not invoke KIO.
    "code_third_party_action": "trash",
    "ocr": "auto",
    "pdf_cache_validation": "metadata",
    "image_document_ocr": "auto",
    # The existing producer already streams bounded batches. An unattended
    # full run has no hidden whole-run ceiling; explicit limits still win.
    "semantic_max_items": None,
    "semantic_max_new_jobs": None,
    "semantic_time_budget_seconds": None,
}

def apply_all_preset(args: argparse.Namespace) -> None:
    """Expand --all without overwriting explicit user options."""

    if not args.all:
        return
    explicit = set(getattr(args, "_explicit_options", ()))
    if "route" in explicit and args.route != "all":
        raise SystemExit(
            "--all selects every built-in route and cannot be combined with "
            "a narrower --route value"
        )
    for name, value in ALL_PRESET.items():
        if name not in explicit:
            setattr(args, name, value)
    # Preset ceilings bound an internal slice, not the whole unattended run.
    # Explicit limits retain their original meaning and never reset per slice.
    args._semantic_complete_all = not bool(
        explicit.intersection(
            {"semantic_max_items", "semantic_max_new_jobs", "semantic_time_budget_seconds"}
        )
    )



# endregion [01]


# region [02] Domain validators


def _validate_global(args: argparse.Namespace) -> None:
    if args.global_memory_budget_mb is not None and args.global_memory_budget_mb < 1:
        raise SystemExit("--global-memory-budget-mb must be positive")
    for name in ("global_min_free_memory_mb", "global_min_free_commit_mb"):
        value = getattr(args, name)
        if value is not None and value < 0:
            raise SystemExit(f"--{name.replace('_', '-')} cannot be negative")
    if args.global_cpu_slots is not None and args.global_cpu_slots < 1:
        raise SystemExit("--global-cpu-slots must be positive")
    if not 0 < args.global_max_cpu_load_percent <= 100:
        raise SystemExit("--global-max-cpu-load-percent must be in (0, 100]")
    if args.global_resource_wait_timeout < 0:
        raise SystemExit("--global-resource-wait-timeout cannot be negative")


def _validate_run_budget(args: argparse.Namespace) -> None:
    """Validate the process-independent limits attached to one Framework run."""

    # The persistence contract accepts zero for item/byte ceilings so callers
    # can deliberately exercise a no-work run.  Keep the CLI integer range
    # within the signed SQLite/JSON envelope used by lifecycle status.
    maximum = (1 << 63) - 1
    for name in ("run_max_items", "run_max_bytes"):
        value = getattr(args, name, None)
        if value is None:
            continue
        if type(value) is not int or not 0 <= value <= maximum:
            option = "--" + name.replace("_", "-")
            raise SystemExit(f"{option} must be a non-negative integer at most {maximum}")

    duration = getattr(args, "run_time_budget_seconds", None)
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or not 0.001 <= float(duration) <= 172_800.0
    ):
        raise SystemExit(
            "--run-time-budget-seconds must be finite and between 0.001 and 172800"
        )


def _validate_image(args: argparse.Namespace) -> None:
    if args.image_workers < 1:
        raise SystemExit("--image-workers must be positive")
    if args.image_max_documents is not None and args.image_max_documents < 1:
        raise SystemExit("--image-max-count must be positive")
    if args.image_memory_budget_mb < 1:
        raise SystemExit("--image-memory-budget-mb must be positive")
    if args.image_min_free_memory_mb < 0 or args.image_min_free_commit_mb < 0:
        raise SystemExit("image memory headroom cannot be negative")
    if args.image_memory_wait_timeout < 0:
        raise SystemExit("--image-memory-wait-timeout cannot be negative")
    if args.image_worker_timeout <= 0:
        raise SystemExit("--image-worker-timeout must be positive")
    if args.image_ocr_timeout <= 0:
        raise SystemExit("--image-ocr-timeout must be positive")
    if args.image_ocr_lang is not None and not args.image_ocr_lang.strip("+"):
        raise SystemExit("--image-ocr-lang must name at least one language")
    if args.image_ocr_lang is not None:
        try:
            parse_language_spec(args.image_ocr_lang)
        except ValueError as exc:
            raise SystemExit(f"--image-ocr-lang: {exc}") from exc


def _validate_pdf_processing(args: argparse.Namespace) -> None:
    try:
        parse_language_spec(args.ocr_lang)
    except ValueError as exc:
        raise SystemExit(f"--ocr-lang: {exc}") from exc
    for name in (
        "pdf_workers",
        "ocr_workers",
        "pdf_min_page_chars",
        "pdf_max_page_text_chars",
        "pdf_max_render_pixels",
        "ocr_timeout",
    ):
        if getattr(args, name) < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.pdf_dpi < 72:
        raise SystemExit("--pdf-dpi must be at least 72")
    if args.max_pdf_pages is not None and args.max_pdf_pages < 1:
        raise SystemExit("--max-pdf-pages must be positive")
    if args.pdf_max_documents is not None and args.pdf_max_documents < 1:
        raise SystemExit("--MaxCount must be positive")
    if args.max_ocr_pages is not None and args.max_ocr_pages < 1:
        raise SystemExit("--max-ocr-pages must be positive")
    if args.pdf_page_start is not None and args.pdf_page_start < 1:
        raise SystemExit("--pdf-page-start must be positive")
    if args.pdf_page_end is not None and args.pdf_page_end < 1:
        raise SystemExit("--pdf-page-end must be positive")
    if (
        args.pdf_page_start is not None
        and args.pdf_page_end is not None
        and args.pdf_page_start > args.pdf_page_end
    ):
        raise SystemExit("--pdf-page-start cannot exceed --pdf-page-end")
    if args.pdf_document_timeout <= 0:
        raise SystemExit("--pdf-document-timeout must be positive")
    if args.pdf_max_document_timeout <= 0:
        raise SystemExit("--pdf-max-document-timeout must be positive")
    if (
        args.pdf_timeout_mode == "adaptive"
        and args.pdf_max_document_timeout < args.pdf_document_timeout
        and "pdf_max_document_timeout" in set(getattr(args, "_explicit_options", ()))
    ):
        raise SystemExit("--pdf-max-document-timeout cannot be below --pdf-document-timeout")


def _validate_pdf_memory(args: argparse.Namespace) -> None:
    for name in (
        "pdf_min_free_bytes",
        "pdf_memory_wait_timeout",
        "pdf_large_document_bytes",
    ):
        if getattr(args, name) < 0:
            raise SystemExit(f"--{name.replace('_', '-')} cannot be negative")
    for name in ("pdf_memory_budget_bytes", "pdf_worker_memory_bytes"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    for name in ("pdf_memory_backpressure_bytes", "pdf_commit_backpressure_bytes"):
        value = getattr(args, name)
        if value is not None and value < 0:
            raise SystemExit(f"--{name.replace('_', '-')} cannot be negative")

    if args.pdf_large_document_workers < 1:
        raise SystemExit("--pdf-large-document-workers must be positive")


def _validate_pdf_queries(args: argparse.Namespace) -> None:
    if not 0.0 <= args.pdf_similarity <= 1.0:
        raise SystemExit("--pdf-similarity must be between 0 and 1")
    if args.pdf_search_limit < 1:
        raise SystemExit("--pdf-search-limit must be positive")
    if args.pdf_search_limit > 1000:
        raise SystemExit("--pdf-search-limit cannot exceed 1000")
    if args.pdf_search is not None and not args.pdf_search.strip():
        raise SystemExit("--pdf-search must be non-empty")
    if args.pdf_layout_groups is not None and not 1 <= args.pdf_layout_groups <= 100:
        raise SystemExit("--pdf-layout-groups must be between 1 and 100")


def _validate_pdf(args: argparse.Namespace) -> None:
    _validate_pdf_processing(args)
    _validate_pdf_memory(args)
    _validate_pdf_queries(args)


def _validate_direct_operation_selection(args: argparse.Namespace) -> None:
    direct_operations = selected_direct_operations(args)
    if len(direct_operations) > 1:
        raise SystemExit(
            "direct status/recovery/review/semantic/curation/PDF/DOCX/Office/ZIP/audio/video/code/"
            "Knowledge "
            "operations are mutually exclusive"
        )
    non_watcher_operations = tuple(
        operation
        for operation in direct_operations
        if operation.family is not DirectOperationFamily.WATCH
    )
    if args.all and non_watcher_operations:
        raise SystemExit("--all cannot be combined with direct query/doctor options")


def _validate_dedupe_operation(args: argparse.Namespace) -> None:
    """Keep the duplicate-service selector separate from routed/direct work."""

    explicit = set(getattr(args, "_explicit_options", ()))
    selected = bool(getattr(args, "dedupe", False))
    if not selected:
        if "dedupe_json" in explicit:
            raise SystemExit("--dedupe-json requires --dedupe")
        return
    if args.all:
        raise SystemExit("--dedupe cannot be combined with --all")
    if normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("--dedupe cannot be combined with --route")
    if args.route_only or args.resume_run is not None or args.candidate_run is not None:
        raise SystemExit("--dedupe cannot be combined with route-only/resume options")
    if selected_direct_operations(args):
        raise SystemExit("--dedupe cannot be combined with direct query/doctor options")


def _validate_json_output(args: argparse.Namespace) -> None:
    """Keep the flat JSON switch attached to a run-producing operation."""

    if not getattr(args, "json_output", False):
        return
    selected_routes = normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER)
    if not (args.all or args.dedupe or selected_routes or args.route_only):
        raise SystemExit("--json requires --all, --dedupe or --route")


def _validate_linux_mutation_capability(args: argparse.Namespace) -> None:
    """Permit Linux effects only when the live platform policy says so.

    The CLI is deliberately not a mutation backend.  A capability check is
    the only gate owned here; the selected service remains responsible for
    identity, containment, and effect verification at its own boundary.
    """

    if not linux_mutation_requested(
        apply=bool(getattr(args, "apply", False)),
        organization_apply=bool(getattr(args, "organization_apply", False)),
    ):
        return
    try:
        policy = current_platform_policy()
        available = bool(getattr(policy, "mutation_available", False))
    except Exception as exc:
        raise SystemExit(
            f"{LINUX_MUTATION_REASON}: mutation capability check failed "
            f"({type(exc).__name__})"
        ) from exc
    if not available:
        raise SystemExit(
            f"{LINUX_MUTATION_REASON}: corpus mutation is unavailable in the active policy"
        )


def _validate_status_operation(args: argparse.Namespace) -> None:
    maximum = 20 if args.status_json else 1000
    if args.status_limit < 1 or args.status_limit > maximum:
        raise SystemExit(f"--status-limit must be between 1 and {maximum}")
    if args.status_run is not None and args.status_run < 1:
        raise SystemExit("--status-run must be positive")
    if (args.status_run is not None or args.status_json) and not args.status:
        raise SystemExit("--status-run and --status-json require --status")
    if args.status and args.apply:
        raise SystemExit("--status is read-only and cannot be combined with --apply")
    if args.status and args.route != "none":
        raise SystemExit("--status cannot be combined with --route")


def _validate_state_health_operation(args: argparse.Namespace) -> None:
    if args.state_health_json and not args.state_health:
        raise SystemExit("--state-health-json requires --state-health")
    explicit = set(getattr(args, "_explicit_options", ()))
    scoped_options = {
        "state_health_scope", "state_health_owner", "state_health_max_owners",
        "state_health_after_owner", "state_health_timeout",
    }
    if scoped_options.intersection(explicit) and not args.state_health:
        raise SystemExit("State health scope options require --state-health")
    timeout = getattr(args, "state_health_timeout", 30.0)
    if not 0 < timeout <= 900:
        raise SystemExit("--state-health-timeout must be greater than 0 and at most 900")
    limit = getattr(args, "state_health_max_owners", None)
    if limit is not None and not 1 <= limit <= 1000:
        raise SystemExit("--state-health-max-owners must be between 1 and 1000")
    if args.state_health and args.apply:
        raise SystemExit("--state-health is read-only and cannot be combined with --apply")
    if args.state_health and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("--state-health cannot be combined with --route")


def _validate_action_recovery_operation(args: argparse.Namespace) -> None:
    recording = args.action_recovery_record is not None
    if getattr(args, "action_recovery_json_lines", False) and not (
        args.action_recovery_json and args.action_recovery_status
    ):
        raise SystemExit("--action-recovery-json-lines requires --action-recovery-status and --action-recovery-json")
    if not 1 <= args.action_recovery_limit <= 1000:
        raise SystemExit("--action-recovery-limit must be between 1 and 1000")
    if args.action_recovery_after < 0:
        raise SystemExit("--action-recovery-after cannot be negative")
    if args.action_recovery_run is not None and args.action_recovery_run < 1:
        raise SystemExit("--action-recovery-run must be positive")
    if recording and args.action_recovery_record < 1:
        raise SystemExit("--action-recovery-record must be positive")
    if args.action_recovery_expected_event is not None and args.action_recovery_expected_event < 1:
        raise SystemExit("--action-recovery-expected-event must be positive")
    explicit = set(getattr(args, "_explicit_options", ()))
    status_options = {
        "action_recovery_limit",
        "action_recovery_after",
        "action_recovery_run",
    }
    if status_options.intersection(explicit) and not args.action_recovery_status:
        raise SystemExit("action recovery page filters require --action-recovery-status")
    record_options = {
        "action_recovery_expected_event",
        "action_recovery_actor",
        "confirm_reconciliation_record",
    }
    if record_options.intersection(explicit) and not recording:
        raise SystemExit("reconciliation record options require --action-recovery-record")
    if "action_recovery_json" in explicit and not (args.action_recovery_status or recording):
        raise SystemExit(
            "--action-recovery-json requires --action-recovery-status or --action-recovery-record"
        )
    if recording and not args.confirm_reconciliation_record:
        raise SystemExit("--action-recovery-record requires --confirm-reconciliation-record")
    if recording and not (args.action_recovery_actor or "").strip():
        raise SystemExit("--action-recovery-record requires --action-recovery-actor")
    if (args.action_recovery_status or recording) and args.apply:
        raise SystemExit("action recovery operations cannot be combined with --apply")
    if (args.action_recovery_status or recording) and normalize_route_selection(
        args.route, BUILTIN_ROUTE_ORDER
    ):
        raise SystemExit("action recovery operations cannot be combined with --route")


def _validate_retention_operation(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    options = {
        "retention_store",
        "retention_batch_size",
        "retention_min_age_days",
        "retention_semantic_after",
        "retention_catalog_after",
        "retention_inventory_after",
        "retention_framework_after",
        "retention_json",
    }
    if options.intersection(explicit) and not args.retention_status:
        raise SystemExit("retention filters and JSON require --retention-status")
    if not 1 <= args.retention_batch_size <= 1000:
        raise SystemExit("--retention-batch-size must be between 1 and 1000")
    stores = tuple(args.retention_store or ())
    if len(stores) != len(set(stores)):
        raise SystemExit("--retention-store values must be unique")
    selected = set(stores or ("semantic", "catalog", "inventory", "framework"))
    for store in ("semantic", "catalog", "inventory", "framework"):
        value = getattr(args, f"retention_{store}_after")
        if value < 0:
            raise SystemExit(f"--retention-{store}-after cannot be negative")
        if f"retention_{store}_after" in explicit and store not in selected:
            raise SystemExit(f"--retention-{store}-after requires --retention-store {store}")
    if args.retention_status and args.apply:
        raise SystemExit("--retention-status is read-only and cannot be combined with --apply")
    if args.retention_status and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("--retention-status cannot be combined with --route")


def _validate_watcher_operation(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    watcher_options = {
        "watch_bootstrap",
        "watch_poll_timeout_seconds",
        "watch_debounce_seconds",
        "watch_max_debounce_seconds",
        "watch_error_backoff_initial_seconds",
        "watch_error_backoff_max_seconds",
        "watch_error_backoff_multiplier",
        "watch_portable_interval_seconds",
    }
    if not args.watch:
        if watcher_options.intersection(explicit):
            raise SystemExit("watcher timing options require --watch")
        return
    if not 1 <= args.watch_poll_timeout_seconds <= 300:
        raise SystemExit("--watch-poll-timeout-seconds must be between 1 and 300")
    if args.watch_debounce_seconds < 0:
        raise SystemExit("--watch-debounce-seconds cannot be negative")
    if args.watch_max_debounce_seconds <= 0:
        raise SystemExit("--watch-max-debounce-seconds must be positive")
    if args.watch_max_debounce_seconds < args.watch_debounce_seconds:
        raise SystemExit("--watch-max-debounce-seconds cannot be below --watch-debounce-seconds")
    if args.watch_error_backoff_initial_seconds < 0:
        raise SystemExit("--watch-error-backoff-initial-seconds cannot be negative")
    if args.watch_error_backoff_max_seconds < args.watch_error_backoff_initial_seconds:
        raise SystemExit("--watch-error-backoff-max-seconds cannot be below the initial backoff")
    if args.watch_error_backoff_multiplier < 1:
        raise SystemExit("--watch-error-backoff-multiplier must be at least 1")
    if (
        not math.isfinite(args.watch_portable_interval_seconds)
        or not 1 <= args.watch_portable_interval_seconds <= 86_400
    ):
        raise SystemExit("--watch-portable-interval-seconds must be between 1 and 86400")
    if args.apply:
        raise SystemExit("--watch cannot be combined with --apply")
    if args.route_only:
        raise SystemExit("--watch cannot be combined with --route-only")
    if args.resume_run is not None:
        raise SystemExit("--watch cannot be combined with --resume-run")
    if args.candidate_run is not None:
        raise SystemExit("--watch cannot be combined with --candidate-run")


def _validate_review_record(args: argparse.Namespace) -> None:
    if args.review_record is None:
        return

    required = {
        "--review-route": args.review_route,
        "--review-reason": args.review_reason,
        "--review-volume-id": args.review_volume_id,
        "--review-file-id": args.review_file_id,
        "--review-generation": args.review_generation,
        "--review-actor": args.review_actor,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise SystemExit("--review-record requires " + ", ".join(missing))
    if not args.review_actor or args.review_actor.strip() != args.review_actor:
        raise SystemExit("--review-actor must be non-empty and trimmed")
    if len(args.review_actor) > 256:
        raise SystemExit("--review-actor cannot exceed 256 characters")
    if args.review_note is not None and (
        not args.review_note or args.review_note.strip() != args.review_note
    ):
        raise SystemExit("--review-note must be non-empty and trimmed")
    if args.review_note is not None and len(args.review_note.encode("utf-8")) > 8 * 1024:
        raise SystemExit("--review-note cannot exceed 8192 UTF-8 bytes")


def _validate_review_limits(args: argparse.Namespace) -> None:
    if args.review_candidates is not None and not 1 <= args.review_candidates <= 10_000:
        raise SystemExit("--review-candidates must be between 1 and 10000")
    if args.review_decisions is not None and not 1 <= args.review_decisions <= 10_000:
        raise SystemExit("--review-decisions must be between 1 and 10000")


def _validate_review_evidence_operations(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    evidence_operation = bool(
        args.review_evidence_sync
        or args.review_evidence_metrics
        or args.review_evidence_list is not None
    )
    if not 1 <= args.review_evidence_batch_size <= 256:
        raise SystemExit("--review-evidence-batch-size must be between 1 and 256")
    if "review_evidence_batch_size" in explicit and not args.review_evidence_sync:
        raise SystemExit("--review-evidence-batch-size requires --review-evidence-sync")
    if args.review_evidence_list is not None and not (1 <= args.review_evidence_list <= 1000):
        raise SystemExit("--review-evidence-list must be between 1 and 1000")
    common_filter_requested = any(
        (
            args.review_evidence_route,
            args.review_evidence_reason,
            args.review_evidence_recommendation,
            args.review_evidence_detector,
            args.review_evidence_actor,
        )
    )
    if common_filter_requested and not (
        args.review_evidence_metrics or args.review_evidence_list is not None
    ):
        raise SystemExit(
            "review evidence filters require --review-evidence-metrics or --review-evidence-list"
        )
    list_filter_requested = bool(args.review_evidence_status or args.review_evidence_completeness)
    if list_filter_requested and args.review_evidence_list is None:
        raise SystemExit(
            "review evidence status/completeness filters require --review-evidence-list"
        )
    review_operation = bool(selected_direct_operations(args, family=DirectOperationFamily.REVIEW))
    if getattr(args, "review_json_lines", False) and not (
        args.review_json and args.review_candidates is not None
    ):
        raise SystemExit("--review-json-lines requires --review-candidates and --review-json")
    if getattr(args, "review_after", None) is not None and args.review_candidates is None:
        raise SystemExit("--review-after requires --review-candidates")
    if getattr(args, "review_after", None) is not None and getattr(args, "review_json_lines", False):
        raise SystemExit("--review-after is unavailable in legacy JSON Lines")
    if args.review_json and not review_operation:
        raise SystemExit("--review-json requires a review command")
    if evidence_operation and args.apply:
        raise SystemExit("review evidence commands cannot be combined with --apply")


def _validate_review_operations(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    _validate_review_limits(args)
    _validate_review_evidence_operations(args, explicit)
    review_operation = any(
        operation.destination in {"review_candidates", "review_decisions", "review_record"}
        for operation in selected_direct_operations(
            args,
            family=DirectOperationFamily.REVIEW,
        )
    )
    candidate_filter_requested = bool(
        args.review_recommendation is not None
        or args.review_status != "open"
        or "review_status" in explicit
    )
    if args.review_candidates is None and candidate_filter_requested:
        raise SystemExit(
            "review filters require --review-candidates (--review-recommendation/--review-status)"
        )
    shared_review_filter_requested = any(
        (
            args.review_route is not None,
            args.review_reason is not None,
            args.review_volume_id is not None,
            args.review_file_id is not None,
            args.review_generation is not None,
            args.review_decision_status is not None,
            args.review_actor is not None,
            args.review_note is not None,
        )
    )
    if not review_operation and shared_review_filter_requested:
        raise SystemExit("review options require a review command")
    if args.review_reason is not None:
        if not args.review_reason or args.review_reason.strip() != args.review_reason:
            raise SystemExit("--review-reason must be non-empty and trimmed")
        if len(args.review_reason) > 256:
            raise SystemExit("--review-reason cannot exceed 256 characters")
    if args.review_generation is not None and args.review_generation < 0:
        raise SystemExit("--review-generation cannot be negative")
    if (args.review_volume_id is None) != (args.review_file_id is None):
        raise SystemExit("--review-volume-id and --review-file-id must be supplied together")
    if args.review_decision_status is not None and args.review_decisions is None:
        raise SystemExit("--review-decision-status requires --review-decisions")

    decision_target_filter = any(
        (
            args.review_reason is not None,
            args.review_volume_id is not None,
            args.review_generation is not None,
        )
    )
    if (
        args.review_candidates is not None
        and args.review_decisions is None
        and args.review_record is None
        and decision_target_filter
    ):
        raise SystemExit("decision identity filters require --review-decisions or --review-record")
    if args.review_actor is not None and args.review_record is None:
        raise SystemExit("--review-actor requires --review-record")
    if args.review_note is not None and args.review_record is None:
        raise SystemExit("--review-note requires --review-record")

    _validate_review_record(args)

    if args.review_candidates is not None and args.apply:
        raise SystemExit("--review-candidates is read-only and cannot be combined with --apply")
    if args.review_decisions is not None and args.apply:
        raise SystemExit("--review-decisions is read-only and cannot be combined with --apply")
    if args.review_record is not None and args.apply:
        raise SystemExit("--review-record cannot be combined with --apply")
    if selected_direct_operations(args, family=DirectOperationFamily.REVIEW) and args.route != "none":
        raise SystemExit("review operations cannot be combined with --route")


def _validate_organization_operations(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    organization_direct = bool(
        selected_direct_operations(args, family=DirectOperationFamily.ORGANIZATION)
    )
    selected_routes = normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER)
    integrated_organization = bool(
        args.apply and ORGANIZABLE_ROUTE_NAMES.intersection(selected_routes)
    )
    if organization_direct and args.apply:
        raise SystemExit("document catalog/organization commands cannot be combined with --apply")
    if args.catalog_preview is not None and not 1 <= args.catalog_preview <= 10_000:
        raise SystemExit("--catalog-preview must be between 1 and 10000")
    catalog_filter_requested = any(
        (
            args.catalog_kind,
            args.catalog_authority,
            args.catalog_organization,
            args.catalog_client,
            args.catalog_project,
            args.catalog_workstream,
        )
    )
    if catalog_filter_requested and args.catalog_preview is None:
        raise SystemExit("catalog filters require --catalog-preview")
    if args.organization_preview is not None and not 1 <= args.organization_preview <= 10_000:
        raise SystemExit("--organization-preview must be between 1 and 10000")
    if args.organization_preview_status is not None and args.organization_preview is None:
        raise SystemExit("--organization-preview-status requires --organization-preview")
    if not 0.0 <= args.organization_min_confidence <= 1.0:
        raise SystemExit("--organization-min-confidence must be between 0 and 1")
    if not 1 <= args.organization_max_actions <= 10_000:
        raise SystemExit("--organization-max-actions must be between 1 and 10000")
    if args.organization_root is not None and not (
        args.organization_plan or args.organization_apply or integrated_organization
    ):
        raise SystemExit("--organization-root requires an organization command or routed --apply")
    if args.no_document_catalog and (
        args.catalog_documents
        or args.organization_plan
        or args.organization_root is not None
        or (integrated_organization and "organization_min_confidence" in explicit)
    ):
        raise SystemExit(
            "--no-document-catalog conflicts with catalog update and organization planning"
        )
    if args.document_taxonomy is not None and not args.document_taxonomy.is_file():
        raise SystemExit("--document-taxonomy must name an existing TOML file")
    if organization_direct and selected_routes:
        raise SystemExit("direct catalog/organization commands cannot be combined with --route")
    if (
        "organization_min_confidence" in explicit
        and not args.organization_plan
        and not integrated_organization
    ):
        raise SystemExit("--organization-min-confidence requires a plan or routed --apply")
    if "organization_max_actions" in explicit and not args.organization_apply:
        raise SystemExit("--organization-max-actions requires --organization-apply")


def _validate_curation_operation(args: argparse.Namespace, explicit: set[str]) -> None:
    operation = bool(
        selected_direct_operations(args, family=DirectOperationFamily.CURATION)
    )
    if args.curation_preview is not None and not 1 <= args.curation_preview <= 10_000:
        raise SystemExit("--curation-preview must be between 1 and 10000")
    if "curation_json" in explicit and args.curation_preview is None:
        raise SystemExit("--curation-json requires --curation-preview")
    if operation and args.apply:
        raise SystemExit("--curation-preview is read-only and cannot be combined with --apply")
    if operation and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("--curation-preview cannot be combined with --route")


def _validate_direct_operations(args: argparse.Namespace) -> None:
    explicit: set[str] = set(getattr(args, "_explicit_options", ()))
    validate_content_diagnostics_arguments(args)
    validate_knowledge_arguments(args)
    _validate_direct_operation_selection(args)
    validate_capabilities_arguments(args)
    validate_config_doctor_arguments(args)
    _validate_status_operation(args)
    _validate_state_health_operation(args)
    _validate_action_recovery_operation(args)
    _validate_retention_operation(args, explicit)
    _validate_watcher_operation(args, explicit)
    _validate_review_operations(args, explicit)
    validate_office_direct_operation(args, explicit)
    validate_docx_direct_operation(args)
    _validate_pdf_direct_operation(args)
    validate_archive_direct_operation(args, explicit)
    _validate_organization_operations(args, explicit)
    _validate_curation_operation(args, explicit)
    validate_audio_direct_operation(args)
    validate_video_direct_operation(args)


def _validate_pdf_direct_operation(args: argparse.Namespace) -> None:
    """Reject route/mutation flags that direct PDF queries cannot consume."""

    if not any(
        (
            args.pdf_search is not None,
            args.pdf_layout_groups is not None,
            args.pdf_doctor,
            args.pdf_verify,
        )
    ):
        return
    if args.apply:
        raise SystemExit("PDF direct actions are read-only and cannot be combined with --apply")
    if args.route != "none":
        raise SystemExit("PDF direct actions cannot be combined with --route")


def _validate_route_only(args: argparse.Namespace) -> None:
    selected_routes = normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER)
    if args.resume_run is not None and args.candidate_run is not None:
        raise SystemExit("--resume-run and --candidate-run are mutually exclusive")
    for name in ("resume_run", "candidate_run"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    route_only = bool(args.route_only or args.resume_run is not None)
    if args.candidate_run is not None and not route_only:
        raise SystemExit("--candidate-run requires --route-only")
    if route_only and not selected_routes and args.resume_run is None:
        raise SystemExit("--route-only requires at least one --route")
    if route_only and args.apply:
        raise SystemExit("--route-only never executes file actions; remove --apply")
    if route_only and args.all:
        raise SystemExit("--route-only cannot be combined with --all")
    selection_requested = bool(
        args.select_status
        or args.select_error_type
        or args.select_recommendation
        or args.select_path
        or args.failed_pages_only
    )
    if selection_requested and not route_only:
        raise SystemExit("explicit route selection requires --route-only or --resume-run")
    if any(not value.strip() for value in args.select_error_type):
        raise SystemExit("--select-error-type cannot be empty")
    if args.failed_pages_only and selected_routes not in {(), ("pdf",)}:
        raise SystemExit("--failed-pages-only can only be used with --route pdf")


# endregion [02]


# region [03] Public validation entry point


def validate_arguments(args: argparse.Namespace) -> None:
    apply_all_preset(args)
    validate_dedup_keeper_arguments(args)
    if args.show_groups < 0:
        raise SystemExit("--show-groups cannot be negative")
    try:
        normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _validate_global(args)
    _validate_run_budget(args)
    _validate_image(args)
    _validate_pdf(args)
    validate_docx_arguments(args)
    validate_office_arguments(args)
    validate_archive_arguments(args)
    validate_text_arguments(args)
    validate_audio_arguments(args)
    validate_video_arguments(args)
    validate_semantic_arguments(args)
    validate_code_arguments(args)
    validate_platform_arguments(args)
    validate_models_arguments(args)
    _validate_direct_operations(args)
    _validate_dedupe_operation(args)
    _validate_json_output(args)
    _validate_linux_mutation_capability(args)
    _validate_route_only(args)


# endregion [03]
