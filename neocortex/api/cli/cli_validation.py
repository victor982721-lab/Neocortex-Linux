"""Preset expansion and bounded command-line validation."""

from __future__ import annotations
import argparse
import math
from pathlib import Path

from neocortex.platform_policy import LINUX_MUTATION_REASON, linux_mutation_requested

from .cli_audio_surface import (
    validate_audio_arguments,
    validate_audio_direct_operation,
)
from .cli_archive_surface import (
    validate_archive_arguments,
    validate_archive_direct_operation,
)
from .cli_capabilities_surface import validate_capabilities_arguments
from .cli_code_surface import validate_code_arguments
from .cli_docx_surface import validate_docx_arguments
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
from neocortex.code.code_contracts import (
    DEFAULT_DEEP_MUTATION_MAX_MUTANTS,
    DEFAULT_DEEP_MUTATION_TIME_BUDGET_SECONDS,
    DEFAULT_DEEP_MUTATION_TIMEOUT_SECONDS,
    normalize_deep_mutation_symbol,
    normalize_deep_mutation_target,
    normalize_deep_test_selectors,
)
from neocortex.safety.corpus_access import path_trees_intersect
from neocortex.safety.ocr_profiles import parse_language_spec
from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER,
    ORGANIZABLE_ROUTE_NAMES,
    normalize_route_selection,
)

# region [01] Stable presets

ALL_PRESET = {
    "route": "all",
    "ocr": "auto",
    "pdf_cache_validation": "metadata",
    "image_document_ocr": "auto",
    # One unattended run must leave useful Semantic progress instead of
    # stopping after the historical 50-item pilot defaults.  The integrated
    # stage still has a hard two-day ceiling and a durable job ceiling.
    "semantic_max_items": 100_000,
    "semantic_max_new_jobs": 1_000_000,
    "semantic_time_budget_seconds": 172_800.0,
}

_SELF_ANALYSIS_UNUSED_PREFIXES = (
    "archive_",
    "audio_",
    "docx_",
    "image_",
    "office_",
    "pdf_",
    "text_",
    "video_",
    "retry_audio_",
    "retry_archive_",
    "retry_docx_",
    "retry_image_",
    "retry_office_",
    "retry_pdf_",
    "retry_text_",
    "retry_video_",
    "semantic_",
    "whisper_",
)
_SELF_ANALYSIS_UNUSED_OPTIONS = frozenset(
    {
        "dedup_policy",
        "document_taxonomy",
        "failed_pages_only",
        "ffprobe_path",
        "max_ocr_pages",
        "max_pdf_pages",
        "ocr",
        "ocr_lang",
        "ocr_profile",
        "ocr_timeout",
        "ocr_workers",
        "organization_min_confidence",
        "organization_root",
        "select_error_type",
        "select_path",
        "select_recommendation",
        "select_status",
        "show_groups",
        "tessdata_dir",
        "tesseract_cmd",
    }
)
_TRUSTED_DEEP_OPTIONS = frozenset(
    {
        "deep_test_selectors",
        "deep_max_tests",
        "deep_time_budget_seconds",
        "deep_shard_size",
        "deep_mutation_target",
        "deep_mutation_symbol",
        "deep_mutation_max_mutants",
        "deep_mutation_timeout_seconds",
        "deep_mutation_time_budget_seconds",
    }
)
_DEEP_MUTATION_OPTIONS = frozenset(
    {
        "deep_mutation_target",
        "deep_mutation_symbol",
        "deep_mutation_max_mutants",
        "deep_mutation_timeout_seconds",
        "deep_mutation_time_budget_seconds",
    }
)


def trusted_deep_expected_root(home: Path | None = None) -> Path:
    """Return the sole project root permitted to execute trusted content."""

    base = Path.home() if home is None else Path(home)
    return base / "Neocortex" / "Repository"


def _is_exact_trusted_deep_root(root: Path) -> bool:
    """Compare physical root identity without any configurable bypass."""

    expected = trusted_deep_expected_root()
    resolved_root = root.resolve(strict=True)
    resolved_expected = expected.resolve(strict=True)
    return resolved_root == resolved_expected and resolved_root.samefile(resolved_expected)


def _validate_self_analysis_mode(
    args: argparse.Namespace,
    explicit_deep: list[str],
) -> bool:
    if args.self_analysis:
        if args.analysis_profile != "trusted-deep" and explicit_deep:
            option = "--" + explicit_deep[0].replace("_", "-")
            raise SystemExit(f"{option} requires --analysis-profile trusted-deep")
        return True
    if "analysis_profile" in getattr(args, "_explicit_options", ()):
        raise SystemExit("--analysis-profile requires --self-analysis")
    if explicit_deep:
        option = "--" + explicit_deep[0].replace("_", "-")
        raise SystemExit(f"{option} requires --self-analysis --analysis-profile trusted-deep")
    return False


def _reject_duplicate_self_analysis_mutations(args: argparse.Namespace) -> None:
    explicit_counts = getattr(args, "_explicit_option_counts", {})
    duplicated_mutation_options = sorted(
        name for name in _DEEP_MUTATION_OPTIONS if explicit_counts.get(name, 0) > 1
    )
    if duplicated_mutation_options:
        option = "--" + duplicated_mutation_options[0].replace("_", "-")
        raise SystemExit(f"{option} cannot be repeated")


def _normalize_self_analysis_deep_controls(args: argparse.Namespace) -> None:
    try:
        args.deep_test_selectors = normalize_deep_test_selectors(args.deep_test_selectors)
    except ValueError as exc:
        raise SystemExit(f"invalid --deep-test-selector: {exc}") from exc
    if not 1 <= args.deep_max_tests <= 10_000:
        raise SystemExit("--deep-max-tests must be between 1 and 10000")
    if not 30 <= args.deep_time_budget_seconds <= 900:
        raise SystemExit("--deep-time-budget-seconds must be between 30 and 900")
    if not 1 <= args.deep_shard_size <= 250:
        raise SystemExit("--deep-shard-size must be between 1 and 250")
    try:
        args.deep_mutation_target = normalize_deep_mutation_target(args.deep_mutation_target)
    except ValueError as exc:
        raise SystemExit(f"invalid --deep-mutation-target: {exc}") from exc
    try:
        args.deep_mutation_symbol = normalize_deep_mutation_symbol(args.deep_mutation_symbol)
    except ValueError as exc:
        raise SystemExit(f"invalid --deep-mutation-symbol: {exc}") from exc


def _validate_self_analysis_deep_limits(args: argparse.Namespace) -> None:
    if not 1 <= args.deep_mutation_max_mutants <= 100:
        raise SystemExit("--deep-mutation-max-mutants must be between 1 and 100")
    if not 1 <= args.deep_mutation_timeout_seconds <= 120:
        raise SystemExit("--deep-mutation-timeout-seconds must be between 1 and 120")
    if not 10 <= args.deep_mutation_time_budget_seconds <= 900:
        raise SystemExit("--deep-mutation-time-budget-seconds must be between 10 and 900")


def _validate_self_analysis_mutation_dependencies(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    explicit_mutation = explicit & _DEEP_MUTATION_OPTIONS
    if args.deep_mutation_symbol is not None and args.deep_mutation_target is None:
        raise SystemExit("--deep-mutation-symbol requires --deep-mutation-target")
    if args.deep_mutation_target is None and explicit_mutation:
        option = "--" + sorted(explicit_mutation)[0].replace("_", "-")
        raise SystemExit(f"{option} requires --deep-mutation-target")
    if args.deep_mutation_target is not None and not args.deep_test_selectors:
        raise SystemExit("--deep-mutation-target requires explicit --deep-test-selector")
    if args.deep_mutation_target is None and (
        args.deep_mutation_max_mutants != DEFAULT_DEEP_MUTATION_MAX_MUTANTS
        or args.deep_mutation_timeout_seconds != DEFAULT_DEEP_MUTATION_TIMEOUT_SECONDS
        or args.deep_mutation_time_budget_seconds != DEFAULT_DEEP_MUTATION_TIME_BUDGET_SECONDS
    ):
        raise SystemExit("deep mutation limits require --deep-mutation-target")


def _validate_self_analysis_deep_controls(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    _reject_duplicate_self_analysis_mutations(args)
    _normalize_self_analysis_deep_controls(args)
    if not 1 <= args.deep_max_tests <= 10_000:
        raise SystemExit("--deep-max-tests must be between 1 and 10000")
    if not 30 <= args.deep_time_budget_seconds <= 900:
        raise SystemExit("--deep-time-budget-seconds must be between 30 and 900")
    if not 1 <= args.deep_shard_size <= 250:
        raise SystemExit("--deep-shard-size must be between 1 and 250")
    _validate_self_analysis_deep_limits(args)
    _validate_self_analysis_mutation_dependencies(args, explicit)


def _validate_self_analysis_required_scope(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    if "root" not in explicit:
        raise SystemExit("--self-analysis requires explicit --root")
    if "state_directory" not in explicit:
        raise SystemExit("--self-analysis requires explicit --state-directory")
    if args.all:
        raise SystemExit("--self-analysis cannot be combined with --all")
    if args.apply:
        raise SystemExit("--self-analysis cannot be combined with --apply")
    if args.route_only or args.candidate_run is not None or args.resume_run is not None:
        raise SystemExit("--self-analysis cannot be combined with route-only or resume controls")
    if selected_direct_operations(args):
        raise SystemExit("--self-analysis cannot be combined with direct operations")


def _validate_self_analysis_route(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    if "route" in explicit:
        try:
            requested_routes = normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if requested_routes != ("code",):
            raise SystemExit("--self-analysis permits only --route code")


def _validate_self_analysis_unused_options(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    unused = sorted(
        name
        for name in explicit
        if name in _SELF_ANALYSIS_UNUSED_OPTIONS or name.startswith(_SELF_ANALYSIS_UNUSED_PREFIXES)
    )
    if unused:
        option = "--" + unused[0].replace("_", "-")
        raise SystemExit(f"{option} is not consumed by --self-analysis")
    if "code_include_generated" in explicit and args.code_include_generated:
        raise SystemExit("--self-analysis rejects --code-generated")
    if "code_include_vendored" in explicit and args.code_include_vendored:
        raise SystemExit("--self-analysis rejects --code-vendored")
    if "code_candidate_scope" in explicit and args.code_candidate_scope != "projects":
        raise SystemExit("--self-analysis rejects --code-scope broad")


def _validate_self_analysis_paths(args: argparse.Namespace) -> None:
    from neocortex.deduplication import InventoryError
    from neocortex.deduplication.inventory.index import validate_inventory_root

    try:
        args.root = validate_inventory_root(args.root)
    except (InventoryError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"invalid --self-analysis root: {exc}") from exc
    if args.analysis_profile == "trusted-deep":
        try:
            exact_trusted_root = _is_exact_trusted_deep_root(args.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(
                "trusted-deep requires the exact canonical root; "
                "canonical root identity cannot be verified: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not exact_trusted_root:
            raise SystemExit(
                f"trusted-deep requires the exact canonical root {trusted_deep_expected_root()}"
            )
    try:
        intersects = path_trees_intersect(args.root, args.state_directory)
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"--self-analysis root/state boundary cannot be verified: {type(exc).__name__}: {exc}"
        ) from exc
    if intersects:
        raise SystemExit("--self-analysis root and state directory must be disjoint")


def apply_self_analysis_preset(args: argparse.Namespace) -> None:
    """Expand the protected code-only preset and reject unused controls."""

    explicit = set(getattr(args, "_explicit_options", ()))
    explicit_deep = sorted(explicit & _TRUSTED_DEEP_OPTIONS)
    if not _validate_self_analysis_mode(args, explicit_deep):
        return
    _validate_self_analysis_deep_controls(args, explicit)
    _validate_self_analysis_required_scope(args, explicit)
    _validate_self_analysis_route(args, explicit)
    _validate_self_analysis_unused_options(args, explicit)
    _validate_self_analysis_paths(args)
    args.route = "code"
    args.no_document_catalog = True
    args.code_candidate_scope = "projects"
    args.code_include_generated = False
    args.code_include_vendored = False


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


def _validate_all_self_analysis_controls(args: argparse.Namespace) -> None:
    if (args.refresh_self_analysis or args.require_fresh_self_analysis) and not args.all:
        raise SystemExit("--refresh-self-analysis and --require-fresh-self-analysis require --all")


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
            "direct status/recovery/review/semantic/PDF/DOCX/Office/ZIP/audio/video/code/"
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


def _validate_status_operation(args: argparse.Namespace) -> None:
    if args.status_limit < 1 or args.status_limit > 1000:
        raise SystemExit("--status-limit must be between 1 and 1000")
    if args.status_run is not None and args.status_run < 1:
        raise SystemExit("--status-run must be positive")
    if (args.status_run is not None or args.status_json) and not args.status:
        raise SystemExit("--status-run and --status-json require --status")
    if args.status and args.apply:
        raise SystemExit("--status is read-only and cannot be combined with --apply")


def _validate_action_recovery_operation(args: argparse.Namespace) -> None:
    recording = args.action_recovery_record is not None
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


def _validate_direct_operations(args: argparse.Namespace) -> None:
    explicit: set[str] = set(getattr(args, "_explicit_options", ()))
    validate_knowledge_arguments(args)
    _validate_direct_operation_selection(args)
    validate_capabilities_arguments(args)
    _validate_status_operation(args)
    _validate_action_recovery_operation(args)
    _validate_retention_operation(args, explicit)
    _validate_watcher_operation(args, explicit)
    _validate_review_operations(args, explicit)
    validate_office_direct_operation(args, explicit)
    validate_archive_direct_operation(args, explicit)
    _validate_organization_operations(args, explicit)
    validate_audio_direct_operation(args)
    validate_video_direct_operation(args)


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
    apply_self_analysis_preset(args)
    apply_all_preset(args)
    _validate_all_self_analysis_controls(args)
    if args.show_groups < 0:
        raise SystemExit("--show-groups cannot be negative")
    try:
        normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _validate_global(args)
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
    if linux_mutation_requested(
        apply=bool(getattr(args, "apply", False)),
        organization_apply=bool(getattr(args, "organization_apply", False)),
    ):
        raise SystemExit(
            f"{LINUX_MUTATION_REASON}: corpus mutation is intentionally unavailable on Linux"
        )
    _validate_route_only(args)


# endregion [03]
