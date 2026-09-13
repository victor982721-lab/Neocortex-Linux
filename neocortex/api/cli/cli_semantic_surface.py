"""Flat argument and validation contract for the multimodal Semantic CLI."""


# region [01] Lightweight imports and public contract

from __future__ import annotations
import argparse
import math
from pathlib import Path

from .cli_operations import DirectOperationFamily, selected_direct_operations

__all__ = ["register_semantic_arguments", "validate_semantic_arguments"]

# endregion [01]


# region [02] Stable flat argument registration


def register_semantic_arguments(parser: argparse.ArgumentParser) -> None:
    """Append the existing flat Semantic group without changing its contract."""

    semantic = parser.add_argument_group("Multimodal semantic index")
    semantic.add_argument(
        "--semantic-status",
        action="store_true",
        help="show bounded read-only semantic state and generation status",
    )
    semantic.add_argument(
        "--semantic-plan",
        choices=("text", "image", "all"),
        help=(
            "plan exact semantic resources, chunks, reuse and bytes; model-time "
            "ranges remain unavailable unless an exact calibration is supplied "
            "through the service API"
        ),
    )
    semantic.add_argument(
        "--semantic-plan-json",
        action="store_true",
        help="emit the read-only semantic plan as one stable JSON document",
    )
    semantic.add_argument(
        "--semantic-plan-max-scratch-bytes",
        type=int,
        default=512 * 1024 * 1024,
        metavar="BYTES",
        help="hard private SQLite scratch-storage ceiling for semantic planning",
    )
    semantic.add_argument(
        "--semantic-prepare-models",
        action="store_true",
        help="explicitly download or load the configured text and CLIP models",
    )
    semantic.add_argument(
        "--semantic-index",
        choices=("text", "image", "all"),
        help="incrementally index existing durable text caches, images, or both",
    )
    semantic.add_argument(
        "--semantic-exact-index-build",
        type=Path,
        metavar="DIRECTORY",
        help="explicitly build a bounded derived exact-vector index in DIRECTORY",
    )
    semantic.add_argument(
        "--semantic-exact-index-model",
        metavar="SIGNATURE",
        help="embedding model signature required by --semantic-exact-index-build",
    )
    semantic.add_argument(
        "--semantic-exact-index-scope",
        choices=("content", "all", "title"),
        default="content",
        help="source scope for an explicit exact-index build (default: content)",
    )
    semantic.add_argument(
        "--semantic-image-calibrate",
        type=Path,
        metavar="DATASET.json",
        help=(
            "measure and persist a bounded local CLIP image-retrieval calibration "
            "from labelled queries"
        ),
    )
    semantic.add_argument(
        "--semantic-max-items",
        type=int,
        default=50,
        metavar="N",
        help=(
            "maximum complete new or changed source items admitted by one "
            "semantic index run; exact replay is free"
        ),
    )
    semantic.add_argument(
        "--semantic-max-new-jobs",
        type=int,
        default=1_500,
        metavar="N",
        help=(
            "maximum durable embedding jobs newly created or reactivated by one "
            "semantic index run; exact replay is free"
        ),
    )
    semantic.add_argument(
        "--semantic-time-budget-seconds",
        type=float,
        default=900.0,
        metavar="SECONDS",
        help="shared monotonic time budget for one semantic index run",
    )
    semantic.add_argument(
        "--semantic-search",
        metavar="QUERY",
        help="search lexical and/or separate semantic vector spaces",
    )
    semantic.add_argument(
        "--semantic-exact-index",
        type=Path,
        metavar="DIRECTORY",
        help=(
            "reuse a prepared exact index for semantic search; cold validation is bounded "
            "and may scan source; only reused handle query is warm"
        ),
    )
    semantic.add_argument(
        "--semantic-classify",
        choices=("text", "image", "all"),
        help="materialize advisory ontology evidence for active embeddings",
    )
    semantic.add_argument(
        "--semantic-evidence",
        metavar="ITEM_ID",
        help="list current advisory ontology evidence for one exact item",
    )
    semantic.add_argument(
        "--semantic-evidence-limit",
        type=int,
        default=100,
        metavar="N",
        help="maximum advisory evidence rows to display; truncation is reported",
    )
    semantic.add_argument(
        "--semantic-source",
        action="append",
        choices=(
            "pdf",
            "docx",
            "xlsx",
            "pptx",
            "odt",
            "audio",
            "archive",
            "text",
            "code",
            "video",
        ),
        help="repeat to select durable text caches for text/all planning or indexing",
    )
    semantic.add_argument(
        "--semantic-text-profile",
        choices=("quality", "compact"),
        default="quality",
        help="quality uses the Spanish Jina model; compact uses MiniLM",
    )
    semantic.add_argument(
        "--semantic-search-mode",
        choices=("all", "text", "image", "lexical"),
        default="all",
        help="rank all spaces by default, or select one independent mode",
    )
    semantic.add_argument(
        "--semantic-search-limit",
        type=int,
        default=20,
        metavar="N",
    )
    semantic.add_argument(
        "--semantic-diagnostic-item", action="append", metavar="ITEM_ID",
        help="trace a known item through the same bounded search (repeat up to 20 times)",
    )
    semantic.add_argument(
        "--semantic-max-vectors",
        type=int,
        default=500_000,
        metavar="N",
        help="hard exact-search scan bound; incomplete results are reported",
    )
    semantic.add_argument(
        "--semantic-model-cache",
        type=Path,
        metavar="DIRECTORY",
    )
    semantic.add_argument("--semantic-threads", type=int, metavar="N")
    semantic.add_argument(
        "--semantic-no-ocr",
        action="store_true",
        help="plan or index visual embeddings without retained image OCR text",
    )
    semantic.add_argument(
        "--semantic-include-compact",
        action="store_true",
        help="also acquire the optional compact text model during preparation",
    )


# endregion [02]


# region [03] Stable validation and error precedence


def _validate_semantic_values(args: argparse.Namespace) -> None:
    if args.semantic_search is not None and not args.semantic_search.strip():
        raise SystemExit("--semantic-search must be non-empty")
    if args.semantic_search is not None and len(args.semantic_search) > 4_096:
        raise SystemExit("--semantic-search cannot exceed 4096 characters")
    if args.semantic_evidence is not None and (
        not args.semantic_evidence or args.semantic_evidence.strip() != args.semantic_evidence
    ):
        raise SystemExit("--semantic-evidence must be non-empty and trimmed")
    if args.semantic_evidence is not None and len(args.semantic_evidence) > 32_768:
        raise SystemExit("--semantic-evidence cannot exceed 32768 characters")
    if not 1 <= args.semantic_evidence_limit <= 1000:
        raise SystemExit("--semantic-evidence-limit must be between 1 and 1000")
    if not 1 <= args.semantic_search_limit <= 1000:
        raise SystemExit("--semantic-search-limit must be between 1 and 1000")
    if not 1 <= args.semantic_max_vectors <= 10_000_000:
        raise SystemExit("--semantic-max-vectors must be between 1 and 10000000")
    if args.semantic_threads is not None and args.semantic_threads < 1:
        raise SystemExit("--semantic-threads must be positive")
    if args.semantic_max_items is None:
        if not args.all:
            raise SystemExit("unlimited Semantic items require --all")
    elif not 1 <= args.semantic_max_items <= 10_000_000:
        raise SystemExit("--semantic-max-items must be between 1 and 10000000")
    if args.semantic_max_new_jobs is None:
        if not args.all:
            raise SystemExit("unlimited Semantic jobs require --all")
    elif not 1 <= args.semantic_max_new_jobs <= 100_000_000:
        raise SystemExit("--semantic-max-new-jobs must be between 1 and 100000000")
    if args.semantic_time_budget_seconds is None:
        if not args.all:
            raise SystemExit("unlimited Semantic time requires --all")
    elif (
        not math.isfinite(args.semantic_time_budget_seconds)
        or not 0.001 <= args.semantic_time_budget_seconds <= 172_800.0
    ):
        raise SystemExit(
            "--semantic-time-budget-seconds must be finite and between 0.001 and 172800"
        )
    if not 64 * 1024 <= args.semantic_plan_max_scratch_bytes <= (16 * 1024 * 1024 * 1024 * 1024):
        raise SystemExit("--semantic-plan-max-scratch-bytes must be between 65536 and 16 TiB")
    explicit = set(getattr(args, "_explicit_options", ()))
    exact_build = getattr(args, "semantic_exact_index_build", None)
    exact_model = getattr(args, "semantic_exact_index_model", None)
    if exact_build is not None:
        if not isinstance(exact_model, str) or not exact_model.strip() or exact_model.strip() != exact_model:
            raise SystemExit(
                "--semantic-exact-index-build requires a non-empty trimmed "
                "--semantic-exact-index-model"
            )
        if len(exact_model) > 4_096:
            raise SystemExit("--semantic-exact-index-model cannot exceed 4096 characters")
        if args.semantic_max_vectors > 500_000:
            raise SystemExit("--semantic-max-vectors for exact-index build cannot exceed 500000")
    else:
        if "semantic_exact_index_model" in explicit:
            raise SystemExit("--semantic-exact-index-model requires --semantic-exact-index-build")
        if "semantic_exact_index_scope" in explicit:
            raise SystemExit("--semantic-exact-index-scope requires --semantic-exact-index-build")
    if getattr(args, "semantic_exact_index", None) is not None and args.semantic_search is None:
        raise SystemExit("--semantic-exact-index requires --semantic-search")


def validate_semantic_arguments(args: argparse.Namespace) -> None:
    """Validate one Semantic selection using the established error order."""

    explicit = set(getattr(args, "_explicit_options", ()))
    semantic_actions = len(selected_direct_operations(args, family=DirectOperationFamily.SEMANTIC))
    integrated_all = bool(args.all)
    code_semantic_search = bool(
        args.code_search is not None
        and any(
            mode in {"semantic", "hybrid"} for mode in tuple(args.code_search_mode or ("hybrid",))
        )
    )
    if semantic_actions > 1:
        raise SystemExit("semantic direct actions are mutually exclusive")
    _validate_semantic_values(args)

    optional_names = {
        "semantic_source",
        "semantic_text_profile",
        "semantic_search_mode",
        "semantic_search_limit",
        "semantic_evidence_limit",
        "semantic_max_vectors",
        "semantic_model_cache",
        "semantic_threads",
        "semantic_no_ocr",
        "semantic_include_compact",
        "semantic_plan_json",
        "semantic_plan_max_scratch_bytes",
        "semantic_max_items",
        "semantic_max_new_jobs",
        "semantic_time_budget_seconds",
        "semantic_exact_index",
        "semantic_exact_index_model",
        "semantic_exact_index_scope",
    }
    code_search_options = (
        {"semantic_model_cache", "semantic_threads"} if code_semantic_search else set()
    )
    integrated_all_options = {
        "semantic_source",
        "semantic_text_profile",
        "semantic_model_cache",
        "semantic_threads",
        "semantic_max_items",
        "semantic_max_new_jobs",
        "semantic_time_budget_seconds",
    }
    unsupported_without_semantic_action = optional_names.intersection(explicit) - (
        code_search_options | (integrated_all_options if integrated_all else set())
    )
    if not semantic_actions and unsupported_without_semantic_action:
        raise SystemExit("semantic options require one semantic direct action")
    text_scope = (
        integrated_all
        or args.semantic_index in {"text", "all"}
        or args.semantic_plan in {"text", "all"}
    )
    if args.semantic_source is not None and not text_scope:
        raise SystemExit("--semantic-source requires semantic text/all planning or indexing")
    image_scope = args.semantic_index in {"image", "all"} or args.semantic_plan in {
        "image",
        "all",
    }
    if args.semantic_no_ocr and not image_scope:
        raise SystemExit("--semantic-no-ocr requires semantic image/all planning or indexing")
    if args.semantic_plan_json and args.semantic_plan is None:
        raise SystemExit("--semantic-plan-json requires --semantic-plan")
    if "semantic_plan_max_scratch_bytes" in explicit and args.semantic_plan is None:
        raise SystemExit("--semantic-plan-max-scratch-bytes requires --semantic-plan")
    if args.semantic_include_compact and not args.semantic_prepare_models:
        raise SystemExit("--semantic-include-compact requires --semantic-prepare-models")
    search_only = {
        "semantic_search_mode",
        "semantic_search_limit",
        "semantic_max_vectors",
        "semantic_diagnostic_item",
        "semantic_exact_index",
    }
    diagnostic_items = getattr(args, "semantic_diagnostic_item", None) or ()
    if len(diagnostic_items) > 20 or any(not item.strip() or len(item) > 512 for item in diagnostic_items):
        raise SystemExit("--semantic-diagnostic-item requires 1..512 characters per item and at most 20 items")
    if (
        search_only.intersection(explicit)
        and args.semantic_search is None
        and getattr(args, "semantic_exact_index_build", None) is None
    ):
        raise SystemExit("semantic search options require --semantic-search")
    if "semantic_evidence_limit" in explicit and args.semantic_evidence is None:
        raise SystemExit("--semantic-evidence-limit requires --semantic-evidence")
    index_only = {
        "semantic_max_items",
        "semantic_max_new_jobs",
        "semantic_time_budget_seconds",
    }
    if index_only.intersection(explicit) and args.semantic_index is None and not integrated_all:
        raise SystemExit("semantic index budget options require --semantic-index")
    model_actions = bool(
        args.semantic_prepare_models
        or args.semantic_index is not None
        or args.semantic_search is not None
        or args.semantic_image_calibrate is not None
        or args.semantic_classify is not None
        or code_semantic_search
        or integrated_all
    )
    if {"semantic_model_cache", "semantic_threads"}.intersection(explicit) and not model_actions:
        raise SystemExit(
            "semantic model cache/thread options require prepare, index, search, or classify"
        )
    if "semantic_text_profile" in explicit and not (
        args.semantic_index is not None
        or args.semantic_plan is not None
        or args.semantic_search is not None
        or args.semantic_classify is not None
        or integrated_all
    ):
        raise SystemExit(
            "--semantic-text-profile requires semantic plan, index, search, or classify"
        )
    if semantic_actions and args.apply:
        raise SystemExit("semantic direct actions cannot be combined with file-action --apply")
    if semantic_actions and args.route != "none":
        raise SystemExit("semantic direct actions cannot be combined with --route")


# endregion [03]
