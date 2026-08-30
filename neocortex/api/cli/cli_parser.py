"""Command-line argument definitions for the NeoCortex framework."""


# region [01] Imports and public contract
# Keep parsing independent from execution so help and validation can be tested
# without starting inventory, route, or persistent-state work.

from __future__ import annotations
import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from neocortex.platform.policy import default_corpus_root

from neocortex import __version__

from neocortex.runtime.config.app_paths import default_state_directory
from .cli_audio_surface import register_audio_arguments
from .cli_archive_surface import register_archive_arguments
from .cli_capabilities_surface import register_capabilities_arguments
from .cli_code_surface import register_code_arguments
from .cli_docx_surface import register_docx_arguments
from .cli_knowledge_surface import register_knowledge_arguments
from .cli_models_surface import register_models_arguments
from .cli_office_surface import register_office_arguments
from .cli_platform_surface import register_platform_arguments
from .cli_semantic_surface import register_semantic_arguments
from .cli_text_surface import register_text_arguments
from .cli_video_surface import register_video_arguments
from neocortex.safety.ocr_profiles import OCR_PROFILE_CHOICES

__all__ = [
    "ExplicitArgumentParser",
    "build_parser",
    "decimal_megabytes",
    "hexadecimal_identifier",
    "retention_days_ns",
]

# endregion [01]

# region [02] Command-line interface
# Define every supported command-line argument, including general inventory,
# deduplication, action-control, PDF extraction, OCR, similarity and search
# options exposed by NeoCortex.


class ExplicitArgumentParser(argparse.ArgumentParser):
    """Remember which destinations the user supplied before presets run."""

    def parse_known_args(self, args=None, namespace=None):
        raw_arguments = list(sys.argv[1:] if args is None else args)
        parsed, extras = super().parse_known_args(raw_arguments, namespace)
        explicit: set[str] = set()
        explicit_counts: dict[str, int] = {}
        for token in raw_arguments:
            if token == "--":
                break
            option = token.split("=", 1)[0]
            action = self._option_string_actions.get(option)
            if action is not None:
                explicit.add(action.dest)
                explicit_counts[action.dest] = explicit_counts.get(action.dest, 0) + 1
        parsed._explicit_options = frozenset(explicit)
        parsed._explicit_option_counts = explicit_counts
        return parsed, extras


def decimal_megabytes(value: str) -> int:
    """Convert a user-facing decimal MB value to an exact byte ceiling."""

    try:
        megabytes = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("MaxMB must be a number") from exc
    if not megabytes.is_finite() or megabytes <= 0:
        raise argparse.ArgumentTypeError("MaxMB must be greater than zero")
    byte_limit = int(megabytes * 1_000_000)
    if byte_limit < 1:
        raise argparse.ArgumentTypeError("MaxMB is too small to represent one byte")
    return byte_limit


def hexadecimal_identifier(value: str) -> int:
    """Parse one non-negative durable file identifier shown by the CLI."""

    if not value or value.strip() != value:
        raise argparse.ArgumentTypeError("identity must be a hexadecimal integer")
    normalized = value[2:] if value.lower().startswith("0x") else value
    if not normalized:
        raise argparse.ArgumentTypeError("identity must be a hexadecimal integer")
    try:
        identity = int(normalized, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identity must be a hexadecimal integer") from exc
    if identity < 0:
        raise argparse.ArgumentTypeError("identity cannot be negative")
    return identity


def retention_days_ns(value: str) -> int:
    """Convert a non-negative decimal day interval to exact nanoseconds."""

    try:
        days = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(
            "retention age must be a non-negative number of days"
        ) from exc
    if not days.is_finite() or days < 0:
        raise argparse.ArgumentTypeError("retention age must be a non-negative number of days")
    nanoseconds = int(days * 86_400_000_000_000)
    if days > 0 and nanoseconds < 1:
        raise argparse.ArgumentTypeError("retention age is below one nanosecond")
    if nanoseconds > 2**63 - 1:
        raise argparse.ArgumentTypeError("retention age exceeds SQLite time range")
    return nanoseconds


def build_parser() -> argparse.ArgumentParser:
    parser = ExplicitArgumentParser(
        prog="Neocortex",
        description="Run the integrated NeoCortex pre-index framework.",
        allow_abbrev=False,
    )
    register_platform_arguments(parser)
    register_models_arguments(parser)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--self-analysis",
        action="store_true",
        help=(
            "analyze one explicitly named source tree as bounded code evidence; "
            "defaults to the protected profile and requires explicit --root and "
            "--state-directory"
        ),
    )
    parser.add_argument(
        "--analysis-profile",
        choices=("protected", "trusted-static", "trusted-deep"),
        default="protected",
        help=(
            "external-evidence profile for --self-analysis; protected is the "
            "safe default, trusted-static loads the explicitly trusted project's "
            "versioned static-analysis policy, and trusted-deep additionally "
            "executes its bounded declared test selection"
        ),
    )
    parser.add_argument(
        "--deep-test-selector",
        dest="deep_test_selectors",
        action="append",
        default=[],
        metavar="TEST_PATH_OR_NODEID",
        help=(
            "repeatable relative tests/ path or pytest node id selected by "
            "trusted-deep; omitting it selects the declared full suite"
        ),
    )
    parser.add_argument(
        "--deep-max-tests",
        type=int,
        default=3000,
        metavar="N",
        help="maximum tests admitted by trusted-deep (1..10000; default 3000)",
    )
    parser.add_argument(
        "--deep-time-budget-seconds",
        type=int,
        default=600,
        metavar="SECONDS",
        help="hard trusted-deep test budget (30..900 seconds; default 600)",
    )
    parser.add_argument(
        "--deep-shard-size",
        type=int,
        default=20,
        metavar="N",
        help="trusted-deep tests per resumable shard (1..250; default 20)",
    )
    parser.add_argument(
        "--deep-mutation-target",
        metavar="RELATIVE_PYTHON_PATH",
        help=(
            "root-relative Python file selected for focal mutation testing; "
            "requires trusted-deep and at least one --deep-test-selector"
        ),
    )
    parser.add_argument(
        "--deep-mutation-symbol",
        metavar="QUALIFIED_NAME",
        help="optional Python symbol or qualified name within --deep-mutation-target",
    )
    parser.add_argument(
        "--deep-mutation-max-mutants",
        type=int,
        default=20,
        metavar="N",
        help="maximum focal mutants generated by trusted-deep (1..100; default 20)",
    )
    parser.add_argument(
        "--deep-mutation-timeout-seconds",
        type=int,
        default=30,
        metavar="SECONDS",
        help="hard timeout per focal mutant (1..120 seconds; default 30)",
    )
    parser.add_argument(
        "--deep-mutation-time-budget-seconds",
        type=int,
        default=600,
        metavar="SECONDS",
        help="hard total focal mutation budget (10..900 seconds; default 600)",
    )

    parser.add_argument(
        "--root",
        type=Path,
        default=default_corpus_root(),
        help="root directory to scan; defaults to the platform corpus root",
    )
    parser.add_argument(
        "--state-directory",
        type=Path,
        default=default_state_directory(),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--show-groups",
        type=int,
        default=0,
        metavar="N",
        help="show the first N detected duplicate groups",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "reuse the exact canonical source-validation receipt when available, then "
            "run every PDF, DOCX, Office, ZIP (including nested ZIP), audio, video, image and "
            "code route using the existing "
            "cache, update the technical catalog and prune stale cache state; with "
            "--apply, also organize every safely classified technical document; "
            "cached errors are "
            "retried only when their explicit --retry-*-errors flag is supplied; "
            "compatible options supplied explicitly override preset defaults"
        ),
    )
    parser.add_argument(
        "--refresh-self-analysis",
        action="store_true",
        help=(
            "explicitly refresh protected Code self-analysis before --all; this is a "
            "producer operation and is never implied by --all"
        ),
    )
    parser.add_argument(
        "--require-fresh-self-analysis",
        action="store_true",
        help=(
            "require --all to find an exact passed validation receipt for the current "
            "clean SHA and published Code review, otherwise fail before corpus work"
        ),
    )
    parser.add_argument(
        "--route",
        default="none",
        metavar="ROUTES",
        help=(
            "content routes after the common inventory: one name, a comma-separated "
            "set such as pdf,docx,office,archive,audio,video,image,code, or all"
        ),
    )
    parser.add_argument(
        "--route-only",
        action="store_true",
        help=(
            "run only selected content routes over durable route inputs; "
            "skip inventory, duplicate planning, content detection and file actions"
        ),
    )
    parser.add_argument(
        "--candidate-run",
        type=int,
        metavar="RUN_ID",
        help=(
            "durable inventory source used by --route-only; defaults to the newest "
            "durable inventory owner without fallback"
        ),
    )
    parser.add_argument(
        "--resume-run",
        type=int,
        metavar="RUN_ID",
        help=(
            "resume incomplete route phases from RUN_ID using its durable route "
            "inputs; this implies --route-only"
        ),
    )
    register_capabilities_arguments(parser)
    status = parser.add_argument_group("Operational status")
    status.add_argument(
        "--status",
        action="store_true",
        help="show bounded read-only run, route and phase status and exit",
    )
    status.add_argument("--status-run", type=int, metavar="RUN_ID")
    status.add_argument("--status-limit", type=int, default=5, metavar="N")
    status.add_argument("--status-json", action="store_true")
    recovery = parser.add_argument_group("Uncertain file-action recovery")
    recovery.add_argument(
        "--action-recovery-status",
        action="store_true",
        help=(
            "classify a bounded page of uncertain file actions without changing "
            "SQLite or repeating a filesystem mutation"
        ),
    )
    recovery.add_argument(
        "--action-recovery-record",
        type=int,
        metavar="ACTION_ID",
        help=(
            "explicitly append the current read-only reconciliation for one "
            "uncertain action; never repeats the filesystem mutation"
        ),
    )
    recovery.add_argument(
        "--action-recovery-expected-event",
        type=int,
        metavar="EVENT_ID",
        help="compare-and-swap predecessor for a later reconciliation observation",
    )
    recovery.add_argument(
        "--action-recovery-actor",
        metavar="ACTOR",
        help="operator or process identity recorded with reconciliation evidence",
    )
    recovery.add_argument(
        "--confirm-reconciliation-record",
        action="store_true",
        help=(
            "authorize the append-only SQLite evidence write and any supported "
            "additive framework schema migration; never authorizes a filesystem "
            "mutation"
        ),
    )
    recovery.add_argument(
        "--action-recovery-limit",
        type=int,
        default=100,
        metavar="N",
    )
    recovery.add_argument(
        "--action-recovery-after",
        type=int,
        default=0,
        metavar="ACTION_ID",
    )
    recovery.add_argument(
        "--action-recovery-run",
        type=int,
        metavar="RUN_ID",
    )
    recovery.add_argument("--action-recovery-json", action="store_true")
    retention = parser.add_argument_group("Read-only retention planning")
    retention.add_argument(
        "--retention-status",
        action="store_true",
        help=(
            "inventory one bounded dry-run page of retention state without "
            "creating, migrating or deleting databases"
        ),
    )
    retention.add_argument(
        "--retention-store",
        action="append",
        choices=("semantic", "catalog", "inventory", "framework"),
        help="repeat to inspect only selected durable stores",
    )
    retention.add_argument(
        "--retention-batch-size",
        type=int,
        default=100,
        metavar="N",
    )
    retention.add_argument(
        "--retention-min-age-days",
        type=retention_days_ns,
        metavar="DAYS",
        help=("explicit dry-run eligibility age; omitted means policy_not_configured"),
    )
    for store in ("semantic", "catalog", "inventory", "framework"):
        retention.add_argument(
            f"--retention-{store}-after",
            type=int,
            default=0,
            metavar="ID",
        )
    retention.add_argument("--retention-json", action="store_true")
    watcher = parser.add_argument_group("Foreground incremental watcher")
    watcher.add_argument(
        "--watch",
        action="store_true",
        help=(
            "observe durable filesystem changes with USN when available and "
            "portable polling otherwise"
        ),
    )
    watcher.add_argument(
        "--watch-bootstrap",
        choices=("always", "if-needed", "never"),
        default="if-needed",
        help="run an initial reconciliation always, only when needed, or never",
    )
    watcher.add_argument(
        "--watch-poll-timeout-seconds",
        type=int,
        default=1,
        metavar="SECONDS",
    )
    watcher.add_argument(
        "--watch-debounce-seconds",
        type=float,
        default=2.0,
        metavar="SECONDS",
    )
    watcher.add_argument(
        "--watch-max-debounce-seconds",
        type=float,
        default=30.0,
        metavar="SECONDS",
    )
    watcher.add_argument(
        "--watch-error-backoff-initial-seconds",
        type=float,
        default=1.0,
        metavar="SECONDS",
    )
    watcher.add_argument(
        "--watch-error-backoff-max-seconds",
        type=float,
        default=60.0,
        metavar="SECONDS",
    )
    watcher.add_argument(
        "--watch-error-backoff-multiplier",
        type=float,
        default=2.0,
        metavar="FACTOR",
    )
    watcher.add_argument(
        "--watch-portable-interval-seconds",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="interval between portable inventory passes when USN is unavailable",
    )
    selection = parser.add_argument_group("Explicit route selection")
    selection.add_argument(
        "--select-status",
        action="append",
        default=[],
        choices=(
            "pending",
            "processing",
            "done",
            "complete",
            "partial",
            "error",
            "protected",
        ),
        help="select only route cache rows in this status; may be repeated",
    )
    selection.add_argument(
        "--select-error-type",
        action="append",
        default=[],
        metavar="TYPE",
        help="select only this stored route error type; may be repeated",
    )
    selection.add_argument(
        "--select-recommendation",
        action="append",
        default=[],
        choices=(
            "retry",
            "keep_protected",
            "manual_review",
            "deletion_candidate",
        ),
        help="select open central review recommendations; may be repeated",
    )
    selection.add_argument(
        "--select-path",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="select one exact candidate path; may be repeated",
    )
    selection.add_argument(
        "--failed-pages-only",
        action="store_true",
        help="PDF only: retry documents that retain retryable failed pages",
    )
    catalog = parser.add_argument_group("Technical document catalog and organization")
    catalog.add_argument(
        "--no-document-catalog",
        action="store_true",
        help="disable automatic PDF/DOCX technical classification after content routes",
    )
    catalog.add_argument(
        "--document-taxonomy",
        type=Path,
        metavar="TOML",
        help="optional TOML additions for authorities and organizations",
    )
    catalog.add_argument(
        "--catalog-documents",
        action="store_true",
        help="update the technical catalog from existing PDF/DOCX caches and exit",
    )
    catalog.add_argument(
        "--catalog-preview",
        type=int,
        metavar="N",
        help="list up to N active technical classifications through a read-only query",
    )
    catalog.add_argument("--catalog-kind", metavar="KIND")
    catalog.add_argument("--catalog-authority", metavar="AUTHORITY")
    catalog.add_argument("--catalog-organization", metavar="COMPANY")
    catalog.add_argument("--catalog-client", metavar="CLIENT")
    catalog.add_argument("--catalog-project", metavar="PROJECT")
    catalog.add_argument("--catalog-workstream", metavar="WORKSTREAM")
    catalog.add_argument(
        "--organization-plan",
        action="store_true",
        help="update classifications and persist safe destination proposals; move nothing",
    )
    catalog.add_argument(
        "--organization-preview",
        type=int,
        metavar="N",
        help="list up to N persisted organization proposals without changing files",
    )
    catalog.add_argument(
        "--organization-preview-status",
        choices=(
            "planned",
            "applying",
            "review",
            "blocked",
            "already_organized",
            "applied",
            "moved_cache_pending",
            "recovery_required",
            "stale",
            "failed",
            "superseded",
        ),
        help="filter --organization-preview by plan status",
    )
    catalog.add_argument(
        "--organization-apply",
        action="store_true",
        help="apply existing snapshot-validated plans without replacing destinations",
    )
    catalog.add_argument(
        "--organization-root",
        type=Path,
        metavar="DIRECTORY",
        help=(
            "destination root for direct organization commands or integrated "
            "--all --apply; defaults to Consulta_Tecnica_Organizada inside the "
            "explicit or latest successfully analyzed --root"
        ),
    )
    catalog.add_argument(
        "--organization-min-confidence",
        type=float,
        default=0.72,
        help="minimum classification confidence admitted to an organization plan",
    )
    catalog.add_argument(
        "--organization-max-actions",
        type=int,
        default=100,
        metavar="N",
        help="maximum already-planned moves applied in one explicit operation",
    )
    global_resources = parser.add_argument_group("Global route coordinator")
    global_resources.add_argument("--global-memory-budget-mb", type=int)
    global_resources.add_argument("--global-min-free-memory-mb", type=int)
    global_resources.add_argument("--global-min-free-commit-mb", type=int)
    global_resources.add_argument("--global-cpu-slots", type=int)
    global_resources.add_argument("--global-max-cpu-load-percent", type=float, default=90.0)
    global_resources.add_argument(
        "--global-resource-wait-timeout",
        type=float,
        default=300.0,
        help=(
            "seconds to wait for live physical/commit headroom when no "
            "NeoCortex work is active; ordinary contention between bounded "
            "route jobs does not expire"
        ),
    )
    image = parser.add_argument_group("Image route")
    image.add_argument("--image-workers", type=int, default=4)
    image.add_argument(
        "--image-max-mb",
        dest="image_max_file_bytes",
        type=decimal_megabytes,
        default=None,
        metavar="MB",
        help="process only images at or below this decimal size",
    )
    image.add_argument(
        "--image-max-count",
        dest="image_max_documents",
        type=int,
        default=None,
        metavar="N",
        help="select at most N eligible images, including cache hits",
    )
    image.add_argument(
        "--retry-image-errors",
        action="store_true",
        help="force one new attempt for unchanged cached image errors",
    )
    image.add_argument("--image-memory-budget-mb", type=int, default=512)
    image.add_argument("--image-min-free-memory-mb", type=int, default=1024)
    image.add_argument("--image-min-free-commit-mb", type=int, default=1024)
    image.add_argument("--image-memory-wait-timeout", type=float, default=60.0)
    image.add_argument("--image-worker-timeout", type=float, default=120.0)
    image.add_argument(
        "--image-document-ocr",
        choices=("auto", "never"),
        default="auto",
        help=(
            "use bounded Tesseract layout/text evidence for document-like images; "
            "auto falls back to pixel heuristics when the runtime is unavailable"
        ),
    )
    image.add_argument(
        "--image-ocr-lang",
        default=None,
        help="image verifier languages; defaults to --ocr-lang",
    )
    image.add_argument(
        "--image-ocr-profile",
        choices=OCR_PROFILE_CHOICES,
        default=None,
        help="image OCR profile; defaults to --ocr-profile",
    )
    image.add_argument(
        "--image-ocr-timeout",
        type=float,
        default=12.0,
        help="hard Tesseract timeout per candidate image",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "authorize identity-bound extension renames and same-volume document "
            "organization moves; Recycle Bin candidates are revalidated but skipped "
            "because the available backend is path-bound; normalized-text PDF "
            "matches and uncertain classifications remain advisory"
        ),
    )
    parser.add_argument(
        "--strict-exit-codes",
        action="store_true",
        help=(
            "return exit code 2 when a completed content route reports document, "
            "page, profile or cached errors; fatal route failures already return "
            "non-zero without this option"
        ),
    )
    review = parser.add_argument_group("Content review")
    review.add_argument(
        "--review-candidates",
        type=int,
        metavar="N",
        help=(
            "list up to N non-destructive content review candidates and exit; "
            "this command never deletes or moves files"
        ),
    )
    review.add_argument(
        "--review-route",
        choices=("pdf", "docx", "office", "audio", "video", "image"),
        help="content route for review queries and decisions",
    )
    review.add_argument(
        "--review-recommendation",
        choices=(
            "retry",
            "keep_protected",
            "manual_review",
            "deletion_candidate",
        ),
        help="filter --review-candidates by its advisory recommendation",
    )
    review.add_argument(
        "--review-status",
        choices=("open", "resolved"),
        default="open",
        help="show open findings by default, or previously resolved findings",
    )
    review.add_argument(
        "--review-decisions",
        type=int,
        metavar="N",
        help="list up to N append-only human review decisions and exit",
    )
    review.add_argument(
        "--review-record",
        choices=("confirmed", "dismissed", "deferred"),
        help="record one human decision about an exact finding generation",
    )
    review.add_argument(
        "--review-decision-status",
        choices=("confirmed", "dismissed", "deferred"),
        help="filter --review-decisions by human decision status",
    )
    review.add_argument(
        "--review-reason",
        metavar="REASON_CODE",
        help="finding reason for a decision query or exact decision target",
    )
    review.add_argument(
        "--review-volume-id",
        type=hexadecimal_identifier,
        metavar="HEX",
        help="durable hexadecimal volume identity shown by review output",
    )
    review.add_argument(
        "--review-file-id",
        type=hexadecimal_identifier,
        metavar="HEX",
        help="durable hexadecimal file identity shown by review output",
    )
    review.add_argument(
        "--review-generation",
        type=int,
        metavar="RUN_ID",
        help="exact finding generation to list or decide",
    )
    review.add_argument(
        "--review-actor",
        metavar="ACTOR",
        help="human or system identity recording --review-record",
    )
    review.add_argument(
        "--review-note",
        metavar="TEXT",
        help="optional bounded note stored with --review-record",
    )
    review.add_argument(
        "--review-evidence-sync",
        action="store_true",
        help="materialize one bounded resumable batch of human-review evidence",
    )
    review.add_argument(
        "--review-evidence-batch-size",
        type=int,
        default=128,
        metavar="N",
        help="decisions scanned by one --review-evidence-sync call (1 to 256)",
    )
    review.add_argument(
        "--review-evidence-metrics",
        action="store_true",
        help="show descriptive review outcomes without claiming calibration",
    )
    review.add_argument(
        "--review-evidence-list",
        type=int,
        metavar="N",
        help="list up to N materialized review-evidence examples",
    )
    review.add_argument(
        "--review-evidence-route",
        choices=("pdf", "docx", "office", "audio", "video", "image"),
    )
    review.add_argument("--review-evidence-reason", metavar="REASON_CODE")
    review.add_argument(
        "--review-evidence-recommendation",
        choices=("retry", "keep_protected", "manual_review", "deletion_candidate"),
    )
    review.add_argument("--review-evidence-detector", metavar="VERSION")
    review.add_argument("--review-evidence-actor", metavar="ACTOR")
    review.add_argument(
        "--review-evidence-status",
        choices=("confirmed", "dismissed", "deferred"),
        help="filter --review-evidence-list by human decision status",
    )
    review.add_argument(
        "--review-evidence-completeness",
        choices=("complete", "incomplete"),
        help="filter listed examples by availability of the candidate evidence snapshot",
    )
    review.add_argument(
        "--review-json",
        action="store_true",
        help="emit review command results as deterministic JSON Lines",
    )
    parser.add_argument(
        "--dedup-policy",
        choices=("fast", "exact"),
        default="fast",
        help=(
            "fast uses equal size plus full XXH3 for planning; Recycle Bin candidates "
            "receive a final byte-for-byte comparison before safe abstention; exact "
            "additionally compares all bytes while planning"
        ),
    )

    pdf = parser.add_argument_group("PDF route")

    pdf.add_argument(
        "--ocr",
        choices=("auto", "never", "always"),
        default="auto",
    )
    pdf.add_argument(
        "--ocr-lang",
        default="spa+eng",
    )
    pdf.add_argument(
        "--ocr-profile",
        choices=OCR_PROFILE_CHOICES,
        default="configured",
        help=(
            "configured preserves --ocr-lang; multilingual profiles preflight "
            "their exact Tesseract packs and use bounded OSD routing"
        ),
    )
    pdf.add_argument(
        "--pdf-dpi",
        type=int,
        default=200,
    )
    pdf.add_argument(
        "--pdf-workers",
        type=int,
        default=4,
    )
    pdf.add_argument(
        "--ocr-workers",
        type=int,
        default=2,
    )
    pdf.add_argument(
        "--pdf-min-page-chars",
        type=int,
        default=40,
    )
    pdf.add_argument(
        "--pdf-max-page-text-chars",
        type=int,
        default=5_000_000,
        help="reject a page result larger than this instead of retaining it in memory",
    )
    pdf.add_argument(
        "--pdf-max-render-pixels",
        type=int,
        default=40_000_000,
        help="maximum rendered OCR pixels per page",
    )
    pdf.add_argument(
        "--max-pdf-pages",
        type=int,
    )
    pdf.add_argument(
        "--MaxMB",
        "--max-mb",
        dest="pdf_max_file_bytes",
        type=decimal_megabytes,
        default=None,
        metavar="MB",
        help=(
            "process only PDFs at or below this decimal size; 1000 MB equals "
            "1 GB; without this option all sizes are eligible"
        ),
    )
    pdf.add_argument(
        "--MaxCount",
        "--max-count",
        dest="pdf_max_documents",
        type=int,
        default=None,
        metavar="N",
        help=(
            "process at most N eligible PDFs in this run; without this option "
            "all eligible PDFs are processed"
        ),
    )
    pdf.add_argument(
        "--max-ocr-pages",
        type=int,
    )
    pdf.add_argument(
        "--ocr-timeout",
        type=int,
        default=120,
    )
    pdf.add_argument(
        "--retry-pdf-errors",
        action="store_true",
        help="force one new attempt for every unchanged cached PDF/page error",
    )
    pdf.add_argument(
        "--no-pdfminer-fallback",
        action="store_true",
    )
    pdf.add_argument(
        "--pdf-similarity",
        type=float,
        default=0.92,
    )
    pdf.add_argument(
        "--pdf-search",
        metavar="QUERY",
    )
    pdf.add_argument(
        "--pdf-search-limit",
        type=int,
        default=20,
    )
    pdf.add_argument(
        "--pdf-cache-validation",
        choices=("metadata", "full"),
        default="metadata",
        help="validate cache by metadata or by the reusable full XXH3 fingerprint",
    )
    pdf.add_argument(
        "--tesseract-cmd",
        help="explicit tesseract path used by PDF, image and video-frame OCR",
    )
    pdf.add_argument(
        "--tessdata-dir",
        help="traineddata directory used by PDF and image OCR",
    )
    pdf.add_argument(
        "--pdf-page-start",
        type=int,
        help="first PDF page to process, using one-based numbering",
    )
    pdf.add_argument(
        "--pdf-page-end",
        type=int,
        help="last PDF page to process, inclusive",
    )
    pdf.add_argument(
        "--pdf-fail-fast-pages",
        action="store_true",
        help="abort a document on its first page error instead of preserving other pages",
    )
    pdf.add_argument(
        "--pdf-document-timeout",
        type=float,
        default=600.0,
        help="base wall-clock timeout per isolated PDF process",
    )
    pdf.add_argument(
        "--pdf-timeout-mode",
        choices=("fixed", "adaptive"),
        default="adaptive",
        help="adapt the PDF timeout to file size and durable page progress by default",
    )
    pdf.add_argument(
        "--pdf-max-document-timeout",
        type=float,
        default=1200.0,
        help="maximum timeout produced by adaptive PDF timeout calculation",
    )
    pdf.add_argument(
        "--pdf-min-free-bytes",
        type=int,
        default=512 * 1024 * 1024,
    )
    pdf.add_argument(
        "--pdf-memory-backpressure-bytes",
        type=int,
        default=None,
        help="minimum free physical memory; defaults to an adaptive system floor",
    )
    pdf.add_argument(
        "--pdf-commit-backpressure-bytes",
        type=int,
        default=None,
        help="minimum free commit capacity; defaults to an adaptive system floor",
    )
    pdf.add_argument(
        "--pdf-memory-budget-bytes",
        type=int,
        default=None,
        help="aggregate PDF worker reservation budget; defaults adaptively",
    )
    pdf.add_argument(
        "--pdf-worker-memory-bytes",
        type=int,
        default=512 * 1024 * 1024,
        help="minimum memory reservation charged to each active PDF worker",
    )
    pdf.add_argument(
        "--pdf-memory-wait-timeout",
        type=float,
        default=60.0,
    )
    pdf.add_argument(
        "--pdf-large-document-bytes",
        type=int,
        default=128 * 1024 * 1024,
    )
    pdf.add_argument(
        "--pdf-large-document-workers",
        type=int,
        default=2,
        help=(
            "maximum concurrent PDFs above the large-document threshold; "
            "memory and commit coordinators still gate admission"
        ),
    )
    direct = pdf.add_mutually_exclusive_group()
    direct.add_argument(
        "--pdf-doctor",
        action="store_true",
        help="check PDF/OCR runtime dependencies and exit without scanning",
    )
    direct.add_argument(
        "--pdf-verify",
        action="store_true",
        help="verify the persistent PDF database and exit without scanning",
    )
    direct.add_argument(
        "--pdf-layout-groups",
        type=int,
        metavar="N",
        help="show up to N active PDF layout families without starting an inventory",
    )

    register_docx_arguments(parser, megabyte_type=decimal_megabytes)

    register_office_arguments(parser, megabyte_type=decimal_megabytes)
    register_archive_arguments(parser, megabyte_type=decimal_megabytes)
    register_text_arguments(parser, megabyte_type=decimal_megabytes)
    register_audio_arguments(parser, megabyte_type=decimal_megabytes)
    register_video_arguments(parser, megabyte_type=decimal_megabytes)

    register_code_arguments(parser, megabyte_type=decimal_megabytes)

    register_semantic_arguments(parser)

    register_knowledge_arguments(parser)

    return parser


# endregion [02]
